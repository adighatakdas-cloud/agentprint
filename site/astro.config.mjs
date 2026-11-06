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
      expressiveCode: { themes: [darkTheme, lightTheme] },
      plugins: [starlightLinksValidator()],
      lastUpdated: false,
      pagination: true,
      // Groups are added as their pages land: Start here, Checks, Reference, Census.
      sidebar: [],
    }),
  ],
});
