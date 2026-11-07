import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import starlightLinksValidator from "starlight-links-validator";
import { darkTheme, lightTheme } from "./ec-themes.mjs";

const repo = "https://github.com/adighatakdas-cloud/agentprint";

export default defineConfig({
  site: "https://adighatakdas-cloud.github.io",
  base: "/agentprint",
  trailingSlash: "always",
  integrations: [
    starlight({
      title: "agentprint",
      logo: {
        light: "./src/assets/logo-small-light.svg",
        dark: "./src/assets/logo-small-dark.svg",
        replacesTitle: true,
      },
      favicon: "/favicon.svg",
      social: [{ icon: "github", label: "GitHub", href: repo }],
      editLink: { baseUrl: `${repo}/edit/main/site/` },
      customCss: ["./src/styles/fonts.css", "./src/styles/brand.css"],
      // The landing page sets `hero`; this draws it (pitch + terminal) instead of the stock hero.
      components: { Hero: "./src/components/Hero.astro" },
      expressiveCode: { themes: [darkTheme, lightTheme] },
      plugins: [starlightLinksValidator()],
      lastUpdated: false,
      pagination: true,
      sidebar: [
        {
          label: "Start here",
          items: [
            { slug: "get-started" },
            { slug: "fix" },
            { slug: "inside-your-agent" },
            { slug: "ci" },
            { slug: "badge" },
          ],
        },
        { label: "Checks", items: [{ autogenerate: { directory: "checks" } }] },
        {
          label: "Reference",
          items: [
            { slug: "agents" },
            { slug: "how-it-works" },
            { slug: "privacy" },
            { slug: "comparison" },
            { slug: "glossary" },
          ],
        },
        { label: "Census", items: [{ slug: "census" }, { slug: "census/pilot-2026-11" }] },
      ],
    }),
  ],
});
