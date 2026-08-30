/** PostCSS pipeline for Tailwind v3. Autoprefixer keeps the CSS usable in the
 *  Edge/Chrome versions that ship on a stock Windows machine. */
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
