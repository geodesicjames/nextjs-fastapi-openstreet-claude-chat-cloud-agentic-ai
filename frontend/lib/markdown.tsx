// A deliberately small Markdown renderer for chat replies: paragraphs,
// bulleted and numbered lists, and **bold**. Everything becomes React
// elements, so text from the model is always escaped (no HTML injection),
// with no dependency to add.
import { Fragment, type ReactNode } from "react";

/** Renders a **bold** run differently (e.g. as a link); null for plain bold. */
export type RenderStrong = (text: string) => ReactNode | null;

function inline(text: string, renderStrong?: RenderStrong): ReactNode[] {
  // Split on **bold** runs; odd indexes are the bold parts.
  return text.split(/\*\*(.+?)\*\*/g).map((part, i) => {
    if (i % 2 === 0) return <Fragment key={i}>{part}</Fragment>;
    return <Fragment key={i}>{renderStrong?.(part) ?? <strong>{part}</strong>}</Fragment>;
  });
}

const BULLET = /^\s*[-*]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;

export function Markdown({ text, renderStrong }: { text: string; renderStrong?: RenderStrong }) {
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push(<p key={blocks.length}>{inline(paragraph.join(" "), renderStrong)}</p>);
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list) {
      const items = list.items.map((item, i) => <li key={i}>{inline(item, renderStrong)}</li>);
      blocks.push(
        list.ordered ? (
          <ol key={blocks.length} className="list-decimal pl-5">
            {items}
          </ol>
        ) : (
          <ul key={blocks.length} className="list-disc pl-5">
            {items}
          </ul>
        ),
      );
      list = null;
    }
  };

  for (const line of text.split("\n")) {
    const bullet = BULLET.exec(line);
    const numbered = NUMBERED.exec(line);
    const item = bullet ?? numbered;
    if (item) {
      flushParagraph();
      const ordered = !bullet;
      if (list && list.ordered !== ordered) flushList();
      list ??= { ordered, items: [] };
      list.items.push(item[1]);
    } else if (line.trim() === "") {
      flushParagraph();
      flushList();
    } else {
      flushList();
      paragraph.push(line.trim());
    }
  }
  flushParagraph();
  flushList();
  return <div className="flex flex-col gap-2">{blocks}</div>;
}
