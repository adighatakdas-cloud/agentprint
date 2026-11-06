// Code-block themes, brand palette only.
import { ExpressiveCodeTheme } from "@astrojs/starlight/expressive-code";

function theme(name, type, c) {
  return new ExpressiveCodeTheme({
    name,
    type,
    colors: {
      "editor.background": c.bg,
      "editor.foreground": c.fg,
      "editorGroupHeader.tabsBackground": c.bg,
      "tab.activeBackground": c.bg,
      "tab.activeBorderTop": c.string,
      "editorGroup.border": c.border,
      "tab.border": c.border,
    },
    settings: [
      { scope: ["comment", "punctuation.definition.comment"], settings: { foreground: c.comment, fontStyle: "italic" } },
      { scope: ["string", "string.quoted"], settings: { foreground: c.string } },
      { scope: ["keyword", "storage", "storage.type", "keyword.control"], settings: { foreground: c.keyword } },
      { scope: ["constant.numeric", "constant.language", "constant"], settings: { foreground: c.number } },
      { scope: ["punctuation", "meta.brace", "keyword.operator"], settings: { foreground: c.fg } },
      { scope: ["source", "text"], settings: { foreground: c.fg } },
    ],
  });
}

export const darkTheme = theme("agentprint-dark", "dark", {
  bg: "#0A1422",
  fg: "#E8EEF5",
  border: "#1A2D44",
  comment: "#8DA2B5",
  string: "#FFB020",
  keyword: "#5CC8FF",
  number: "#FF7A2F",
});

export const lightTheme = theme("agentprint-light", "light", {
  bg: "#FFFFFF",
  fg: "#06121F",
  border: "#C9D8E4",
  comment: "#4A5E72",
  string: "#A35F00",
  keyword: "#0B6FA4",
  number: "#B8480A",
});
