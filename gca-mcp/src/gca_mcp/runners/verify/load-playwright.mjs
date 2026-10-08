import { execSync } from 'node:child_process';
import { createRequire } from 'node:module';

async function importPlaywright() {
  if (!process.env.NODE_PATH) {
    try {
      const globalRoot = execSync('npm root -g', { encoding: 'utf8' }).trim();
      process.env.NODE_PATH = globalRoot;
      const Module = await import('node:module');
      Module.default?._initPaths?.();
    } catch {
      // The normal package import below still reports the actionable failure.
    }
  }

  try {
    return await import('playwright');
  } catch {
    const require = createRequire(import.meta.url);
    const globalRoot = execSync('npm root -g', { encoding: 'utf8' }).trim();
    return require(`${globalRoot}/playwright`);
  }
}

const playwright = await importPlaywright();

export const chromium = playwright.chromium;
