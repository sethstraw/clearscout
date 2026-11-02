import js from "@eslint/js";
import tanstackQuery from "@tanstack/eslint-plugin-query";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "src/api/schema.d.ts"] },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  jsxA11y.flatConfigs.recommended,
  reactHooks.configs.flat.recommended,
  ...tanstackQuery.configs["flat/recommended"],
  reactRefresh.configs.vite,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    rules: {
      "@typescript-eslint/consistent-type-imports": "error",
    },
  },
  // No tsconfig includes the JavaScript files, so type-aware rules have nothing to read.
  { files: ["**/*.js"], extends: [tseslint.configs.disableTypeChecked] },
);
