import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#0b0f14",
        panel: "#111823",
        edge: "#1e2a3a",
        glow: "#22d3ee",
      },
    },
  },
  plugins: [],
};
export default config;
