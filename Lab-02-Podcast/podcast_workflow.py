"""
Microsoft Agent Framework Workflow - Podcast Generator
使用 GitHub Copilot 作为 LLM 提供方，结合 MAF Workflow 进行编排

播客名称：《世界杯的每日播报》

工作流拓扑（顺序执行）：
    PodcastSearchExecutor -> PodcastContentExecutor -> PodcastScriptExecutor
    (爬虫抓取新闻+生成大纲)  (生成脚本草稿)             (润色并保存)

PodcastSearchExecutor 不再依赖 LLM 凭空生成大纲，而是：
    1. 从 https://sports.cctv.com/football/international/ 爬取 5 条最新美加墨世界杯新闻链接
    2. 逐条读取新闻正文内容
    3. 对内容进行提取后，交给 GitHub Copilot 生成《世界杯的每日播报》播客大纲

环境变量（可选）：
- GITHUB_COPILOT_CLI_PATH - Copilot CLI 可执行文件路径
- GITHUB_COPILOT_MODEL    - 使用的模型（如 "gpt-5", "claude-sonnet-4"）
- GITHUB_COPILOT_TIMEOUT  - 请求超时（秒）
"""

import asyncio
import argparse
import html
import re
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing_extensions import Never

from agent_framework import (
    Executor,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)
from agent_framework.github import GitHubCopilotAgent
from dotenv import load_dotenv


PODCAST_NAME = "世界杯的每日播报"
NEWS_LIST_URL = "https://sports.cctv.com/football/international/"
NEWS_COUNT = 5

# 美加墨世界杯相关关键词，用于从列表页筛选新闻链接
WORLD_CUP_KEYWORDS = ("世界杯", "美加墨")

# CCTV 文章详情页 URL 模式（ARTI = 图文，VIDE = 视频，均为 .shtml）
ARTICLE_URL_PATTERN = re.compile(
    r"https?://(?:sports|tv|worldcup)\.cctv\.com/\d{4}/\d{2}/\d{2}/[A-Za-z0-9]+\.shtml",
    flags=re.IGNORECASE,
)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}


@dataclass(frozen=True)
class NewsArticle:
    """从详情页读取并提取后的新闻内容"""

    index: int
    url: str
    title: str
    content: str


def save_podcast_content(content: str, output_dir: str = "podcast") -> str:
    """保存播客内容到文件"""
    podcast_dir = Path(output_dir)
    podcast_dir.mkdir(exist_ok=True)

    file_uuid = str(uuid.uuid4())[:8]
    filename = f"2p_podcast_{file_uuid}.txt"
    file_path = podcast_dir / filename

    file_path.write_text(content, encoding="utf-8")
    print(f"内容已保存到文件: {file_path}")
    return str(file_path)


# ---------------------------------------------------------------------------
# 爬虫工具 — 抓取新闻列表与文章正文
# ---------------------------------------------------------------------------


def normalize_space(value: str) -> str:
    """折叠空白并反转义 HTML 实体"""
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def fetch_html(url: str, timeout: int = 30) -> str:
    """抓取页面 HTML 文本"""
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


# 页脚/版权等模板文本标记，提取正文时跳过
BOILERPLATE_MARKERS = ("京ICP备", "版权所有", "中央广播电视总台", "全站地图", "正在加载")


def _is_boilerplate(text: str) -> bool:
    return any(marker in text for marker in BOILERPLATE_MARKERS)


class ListParser(HTMLParser):
    """解析列表页，收集 (href, 链接文本) 对"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attr_map = {name.lower(): (value or "") for name, value in attrs}
        self._current_href = attr_map.get("href", "")
        self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            text = normalize_space(data)
            if text:
                self._current_text.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current_href is None:
            return
        href = self._current_href.strip()
        text = normalize_space(" ".join(self._current_text))
        if href:
            self.links.append((href, text))
        self._current_href = None
        self._current_text = []


def extract_article_urls(list_html: str) -> list[str]:
    """从列表页 HTML 中提取所有唯一的文章详情页链接（保留页面顺序）

    列表页部分标题由 JS 动态渲染，静态 HTML 中拿不到文本，
    因此这里只收集 URL，是否为美加墨世界杯新闻由详情页标题/正文再判断。
    """
    parser = ListParser()
    parser.feed(list_html)

    seen_urls: set[str] = set()
    urls: list[str] = []
    for href, _text in parser.links:
        href = href.strip()
        if not ARTICLE_URL_PATTERN.fullmatch(href):
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        urls.append(href)
    return urls


class ArticleParser(HTMLParser):
    """解析文章详情页，提取标题与正文段落"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.paragraphs: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._in_paragraph = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif name == "title":
            self._in_title = True
        elif name == "p":
            self._in_paragraph = True
            self._buffer = []
        elif name == "meta":
            attr_map = {a.lower(): (v or "") for a, v in attrs}
            if attr_map.get("property", "").lower() == "og:title":
                content = normalize_space(attr_map.get("content", ""))
                if content and not self.title:
                    self.title = content

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif name == "title":
            self._in_title = False
        elif name == "p" and self._in_paragraph:
            text = normalize_space(" ".join(self._buffer))
            if len(text) >= 8 and not _is_boilerplate(text):
                self.paragraphs.append(text)
            self._in_paragraph = False
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title and not self.title:
            self.title = normalize_space(f"{self.title} {data}")
        elif self._in_paragraph:
            self._buffer.append(data)


def fetch_article(url: str, max_chars: int = 4000) -> NewsArticle | None:
    """读取单条新闻详情并提取正文内容；读取失败返回 None"""
    print(f"  -> 正在读取: {url}")
    try:
        raw_html = fetch_html(url)
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        print(f"  !! 读取失败: {error}")
        return None

    parser = ArticleParser()
    parser.feed(raw_html)
    title = parser.title or "世界杯新闻"
    body = "\n".join(parser.paragraphs).strip() or title
    if len(body) > max_chars:
        body = body[:max_chars].rsplit("\n", 1)[0]
    return NewsArticle(index=0, url=url, title=title, content=body)


def is_world_cup_article(article: NewsArticle) -> bool:
    """根据详情页标题 + 正文判断是否为美加墨世界杯新闻"""
    haystack = f"{article.title}\n{article.content}"
    return any(keyword in haystack for keyword in WORLD_CUP_KEYWORDS)


async def crawl_world_cup_news(count: int = NEWS_COUNT) -> list[NewsArticle]:
    """爬取最新的美加墨世界杯新闻并读取正文

    优先选取含世界杯/美加墨关键词的新闻；若不足 count 条，
    再用最新的其他国际足球新闻补齐，尽量凑足 count 条。
    """
    print(f"正在抓取新闻列表: {NEWS_LIST_URL}")
    list_html = await asyncio.to_thread(fetch_html, NEWS_LIST_URL)
    candidate_urls = extract_article_urls(list_html)
    if not candidate_urls:
        raise RuntimeError("未能从央视体育国际足球频道提取到任何新闻链接")

    print(f"已提取 {len(candidate_urls)} 条候选链接，正在逐条读取并筛选美加墨世界杯新闻")
    fetched = await asyncio.gather(
        *(asyncio.to_thread(fetch_article, url) for url in candidate_urls)
    )
    available = [article for article in fetched if article is not None]

    world_cup = [article for article in available if is_world_cup_article(article)]
    others = [article for article in available if not is_world_cup_article(article)]
    selected = (world_cup + others)[:count]
    if not selected:
        raise RuntimeError("未能读取到任何新闻正文")

    return [
        NewsArticle(index=i + 1, url=a.url, title=a.title, content=a.content)
        for i, a in enumerate(selected)
    ]


def build_news_digest(articles: list[NewsArticle]) -> str:
    """把爬取到的新闻内容拼接为提供给 LLM 的摘要文本"""
    sections = [f"以下是爬虫从央视体育抓取的 {len(articles)} 条最新美加墨世界杯新闻："]
    for article in articles:
        sections.append(
            f"\n【新闻 {article.index}】{article.title}\n"
            f"链接：{article.url}\n"
            f"正文：\n{article.content}"
        )
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Executor 定义 — 每个 Executor 封装一个 GitHubCopilotAgent 调用
# ---------------------------------------------------------------------------


class PodcastSearchExecutor(Executor):
    """播客搜索 Executor：爬取美加墨世界杯新闻并生成播客大纲"""

    def __init__(self):
        super().__init__(id="podcast-search-agent")

    @handler
    async def generate_outline(self, topic: str, ctx: WorkflowContext[str]) -> None:
        # 1. 爬虫抓取 5 条最新美加墨世界杯新闻并读取正文
        articles = await crawl_world_cup_news(NEWS_COUNT)
        news_digest = build_news_digest(articles)
        print(f"\n[podcast-search-agent] 已读取 {len(articles)} 条新闻内容")

        # 2. 基于提取后的新闻内容生成《世界杯的每日播报》大纲
        agent = GitHubCopilotAgent(
            instructions=(
                f"你是一位专业的体育播客内容策划人，负责《{PODCAST_NAME}》这档每日世界杯播报节目。"
                "请根据用户提供的、由爬虫抓取的最新美加墨世界杯新闻内容，"
                "提炼要点并生成一份详细的播客大纲，包括：\n"
                f"1. 开场引入（点明节目名《{PODCAST_NAME}》及本期日期主题）\n"
                "2. 逐条新闻要点（覆盖提供的每条新闻，提炼关键信息，不要杜撞新闻中没有的事实）\n"
                "3. 结尾总结与下期预告\n"
                "请只依据提供的新闻内容撰写，用中文回复。"
            ),
            name="podcast-search-agent",
        )
        async with agent:
            result = await agent.run(
                f"播客主题：{topic}\n\n{news_digest}\n\n"
                f"请据此生成《{PODCAST_NAME}》的播客大纲。"
            )

        outline = str(result)
        print(f"\n[podcast-search-agent] 播客大纲已生成")
        await ctx.send_message(outline)


class PodcastContentExecutor(Executor):
    """播客内容 Agent：根据大纲生成两人对话风格播客脚本"""

    def __init__(self):
        super().__init__(id="podcast-content-agent")

    @handler
    async def generate_script(self, outline: str, ctx: WorkflowContext[str]) -> None:
        agent = GitHubCopilotAgent(
            instructions=(
                f"你是《{PODCAST_NAME}》的专业播客撰稿人，要写一档双人对谈风格的体育播客脚本。\n"
                "角色：Host（主持人，担当黄健翔式的激情、接地气、爱抛梗）、"
                "Guest（嘉宾，担当贺炜式的文艺、儒雅、善用比喻和金句）。\n"
                "风格要求：\n"
                "1. 风趣幽默、轻松接地气，像两个懂球的朋友在聊天；\n"
                "2. 语言简单易懂，少用专业术语，必要时用生活化的比喻解释；\n"
                "3. 每个人每次发言不超过 2-3 句话，你来我往、节奏明快；\n"
                "4. 适当抖包袱、互相调侃，但内容要紧扣每条世界杯新闻要点，不杜撰事实；\n"
                "5. 用「Host:」「Guest:」标明说话人。\n"
                "请用中文回复。"
            ),
            name="podcast-content-agent",
        )
        async with agent:
            result = await agent.run(
                f"请根据以下《{PODCAST_NAME}》播客大纲撰写完整的双人对谈脚本，"
                "Host 模仿黄健翔的激情接地气，Guest 模仿贺炜的文艺金句，"
                "两人轮流发言、每次不超过 2-3 句、风趣幽默、简单易懂：\n\n"
                f"{outline}"
            )

        content = str(result)
        print(f"\n[podcast-content-agent] 播客脚本草稿已生成")
        await ctx.send_message(content)


class PodcastScriptExecutor(Executor):
    """播客脚本 Agent：润色并保存最终播客脚本"""

    def __init__(self):
        super().__init__(id="podcast-script-agent")

    @handler
    async def finalize_script(self, draft: str, ctx: WorkflowContext[Never, str]) -> None:
        agent = GitHubCopilotAgent(
            instructions=(
                f"你是《{PODCAST_NAME}》的播客脚本编辑。对提供的双人对谈脚本草稿进行最终润色，"
                "保持 Host（黄健翔式激情接地气）与 Guest（贺炜式文艺金句）的双人风趣幽默风格，"
                "确保语言简单易懂、每人每次发言不超过 2-3 句、对话节奏明快、开场和结尾完整。\n"
                "请直接输出最终版本的脚本，不要添加额外说明。请用中文回复。"
            ),
            name="podcast-script-agent",
        )
        async with agent:
            result = await agent.run(
                f"请润色以下《{PODCAST_NAME}》播客脚本并输出最终版本：\n\n{draft}"
            )

        final_script = str(result)
        save_podcast_content(final_script)
        print(f"\n[podcast-script-agent] 最终播客脚本已保存")
        await ctx.yield_output(final_script)


# ---------------------------------------------------------------------------
# Workflow 构建与运行
# ---------------------------------------------------------------------------


def create_podcast_workflow() -> Workflow:
    """
    创建播客生成工作流

    使用 WorkflowBuilder 将三个 Executor 按顺序串联：
        search(爬虫+大纲) -> content -> script
    """
    search = PodcastSearchExecutor()
    content = PodcastContentExecutor()
    script = PodcastScriptExecutor()

    return (
        WorkflowBuilder(start_executor=search)
        .add_edge(search, content)
        .add_edge(content, script)
        .build()
    )


async def run_podcast_workflow(input_topic: str) -> str:
    """运行播客生成工作流并通过流式事件输出进度"""
    workflow = create_podcast_workflow()

    print(f"开始生成《{PODCAST_NAME}》播客内容，主题: {input_topic}")
    print("=" * 60)

    outputs: list[str] = []
    async for event in workflow.run(input_topic, stream=True):
        if event.type == "executor_invoked":
            executor_id = getattr(event, "executor_id", "")
            print(f"  -> 正在执行: {executor_id}")
        elif event.type == "executor_completed":
            executor_id = getattr(event, "executor_id", "")
            print(f"  <- 完成执行: {executor_id}")
        elif event.type == "output":
            outputs.append(cast(str, event.data))

    print("=" * 60)
    print("工作流执行完成！")
    return outputs[0] if outputs else ""


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description=f"生成《{PODCAST_NAME}》播客内容的工作流脚本（爬虫 + GitHub Copilot + MAF Workflow）"
    )
    parser.add_argument(
        "--topic", "-t",
        type=str,
        default="美加墨世界杯每日新闻播报",
        help="播客主题（默认：美加墨世界杯每日新闻播报）",
    )
    args = parser.parse_args()

    load_dotenv(".env")

    result = asyncio.run(run_podcast_workflow(input_topic=args.topic))

    if result:
        print(f"\n《{PODCAST_NAME}》播客内容生成完成!")


if __name__ == "__main__":
    main()
