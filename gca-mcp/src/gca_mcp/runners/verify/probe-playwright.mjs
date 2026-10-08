import { existsSync } from 'node:fs';

import { chromium } from './load-playwright.mjs';

const executable = chromium.executablePath();
if (!existsSync(executable)) {
  console.error(`browser_missing:${executable}`);
  process.exit(2);
}
console.log(`ok:${executable}`);
