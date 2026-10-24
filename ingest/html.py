import re
from html.parser import HTMLParser

BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "table"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            cells = [c for c in self.row if c]
            if cells:
                self.parts.append("\n" + " | ".join(cells) + "\n")
            self.row = None
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)
        else:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Plain text with one paragraph per line; table rows become "cell | cell"."""
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    text = "\n".join(line for line in lines if line)
    return re.sub(r"\n{2,}", "\n", text).strip()
