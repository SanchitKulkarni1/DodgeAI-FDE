/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        canvas: '#0f1117',
        surface: '#161922',
        surfaceHighlight: '#222631',
        entity: {
          customer: '#4A90E2', // matching API
          salesOrder: '#7ED321', // matching API
          delivery: '#F5A623', // matching API
          billingDoc: '#D0021B', // matching API
          journalEntry: '#9B59B6', // matching API
          payment: '#1ABC9C', // matching API
          product: '#E67E22', // matching API
          plant: '#95A5A6', // matching API
        }
      }
    },
  },
  plugins: [],
}
