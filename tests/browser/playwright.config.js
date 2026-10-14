const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({
  testDir: '.',
  testMatch: '*.spec.js',
  fullyParallel: true,
  workers: 2,
  use: { browserName: 'chromium', headless: true },
  reporter: 'list',
});
