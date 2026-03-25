/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        mono: ['"DM Mono"', 'monospace'],
        sans: ['Syne', 'sans-serif'],
      },
      colors: {
        canvas: '#0d0f14',
        surface: '#111318',
        surfaceHighlight: '#1a1d27',
        entity: {
          order:        '#f97316',
          delivery:     '#22c55e',
          invoice:      '#3b82f6',
          payment:      '#a855f7',
          customer:     '#06b6d4',
          product:      '#eab308',
          address:      '#6b7280',
          journalEntry: '#ec4899',
        }
      }
    },
  },
  plugins: [],
}
