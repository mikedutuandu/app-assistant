import { describe, expect, it } from "vitest";
import { renderMarkdown } from "../src/markdown";

// Mike, 21 Nov 2026: answers showed "- Series" and **bold** as typed. The widget draws them as the reader expects,
// with DOM nodes only, so nothing in an answer can run in the page.
const html = (text: string) => {
  const box = document.createElement("div");
  box.append(renderMarkdown(text));
  return box.innerHTML;
};

describe("renderMarkdown", () => {
  it("draws paragraphs, keeping a single line break", () => {
    expect(html("One line\nsame paragraph\n\nNext one")).toBe("<p>One line<br>same paragraph</p><p>Next one</p>");
  });

  it("draws bullet and numbered lists, one level nested", () => {
    expect(html("You have 9 videos:\n- 5 in Day 1–5\n  - Day 2 best\n- 1 intro")).toBe(
      "<p>You have 9 videos:</p><ul><li>5 in Day 1–5<ul><li>Day 2 best</li></ul></li><li>1 intro</li></ul>",
    );
    expect(html("3. Film\n4. Edit")).toBe('<ol start="3"><li>Film</li><li>Edit</li></ol>');
    expect(html("• one\n• two")).toBe("<ul><li>one</li><li>two</li></ul>");
  });

  it("draws bold, italic, code and safe links; a heading is a bold line", () => {
    expect(html("**Best post:** 732 views, *3.5×* your `normal`")).toBe(
      "<p><strong>Best post:</strong> 732 views, <em>3.5×</em> your <code>normal</code></p>",
    );
    expect(html("[Billing](https://app.example/billing)")).toBe(
      '<p><a href="https://app.example/billing" target="_blank" rel="noreferrer">Billing</a></p>',
    );
    expect(html("## This week")).toBe("<p><strong>This week</strong></p>");
  });

  it("never runs what the text says: HTML stays text, an unsafe link stays as written", () => {
    expect(html('<img src=x onerror="alert(1)"> hi')).toBe('<p>&lt;img src=x onerror="alert(1)"&gt; hi</p>');
    expect(html("[click](javascript:alert(1))")).toBe("<p>[click](javascript:alert(1))</p>");
  });

  it("leaves a lone star or underscore as it is", () => {
    expect(html("5 * 3 = 15 and snake_case_name")).toBe("<p>5 * 3 = 15 and snake_case_name</p>");
  });
});
