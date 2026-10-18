/*
  Rebuilds og.jpg — the picture that shows up when the link is pasted in
  Messenger, WhatsApp or Facebook.

  The three cards in it are drawn by the site's own card renderer, so the
  preview always matches what people actually get.

  Run from the repo root, with a local server on :8777:
      python3 -m http.server 8777 &
      npx playwright@1.56.1 install chromium      # first time only
      node scripts/make-og.mjs
*/
import { chromium } from 'playwright';
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const BASE = process.env.OG_BASE || 'http://localhost:8777';
const CARDS = [['ticket', 'pink'], ['cricket', 'green'], ['blob', 'cream']];

const SAMPLE_A = {
  name: 'শাকিল', score: 8, three: 'ব্যস্ত, শেখা, সাহস',
  happy: 'প্রথম বেতনে মাকে শাড়ি কিনে দিলাম', learn: 'রান্না',
  travel: 'সিলেট, কক্সবাজার', food: 'কাচ্চি', song: 'কোক স্টুডিও বাংলা',
  person: 'আম্মু', win: 'চাকরিটা পেলাম', next: 'নিজের একটা ঘর', tone: 'মজার',
};
const SAMPLE_R = {
  title: 'যে বছর আমি নিজেকে চিনলাম', opening: 'শুরুটা এলোমেলো ছিল।',
  words: ['স্মৃতি', 'মানুষ', 'নতুন শুরু'], scoreLine: 'ভালোই গেছে',
  cardLine: 'নিজেকে নতুন করে চিনেছি', chapters: [], awards: [], closing: '', notes: [],
};

const browser = await chromium.launch();

// 1. let the site draw the sample cards
const site = await browser.newPage({ viewport: { width: 1200, height: 900 } });
await site.goto(`${BASE}/index.html`, { waitUntil: 'networkidle' });
await site.waitForTimeout(2500);
const cards = await site.evaluate(async ({ a, r, picks }) => {
  A = a; R = r; initCS();
  const c = document.createElement('canvas');
  c.width = 1080;
  await loadFonts();
  const out = [];
  for (const [layout, theme] of picks) {
    Object.assign(CS, { layout, theme, font: (REC[layout] || REC._)[0], showTag: true });
    await ensureFont(CS.font);
    drawCard(c);
    out.push(c.toDataURL('image/png'));
  }
  return out;
}, { a: SAMPLE_A, r: SAMPLE_R, picks: CARDS });
await site.close();

// 2. drop them into the template and shoot it
const og = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
await og.goto(`${BASE}/scripts/og-template.html`, { waitUntil: 'networkidle' });
await og.evaluate(srcs => srcs.forEach((s, i) => { document.getElementById('c' + i).src = s; }), cards);
await og.waitForFunction(() => [...document.querySelectorAll('.right img')].every(i => i.complete && i.naturalWidth));
await og.evaluate(() => document.fonts.ready);
await og.waitForTimeout(600);
// JPEG, not PNG: the PNG of this came out at 620KB and WhatsApp quietly drops
// previews over its size limit. q90 is 138KB and visually identical here.
writeFileSync(join(ROOT, 'og.jpg'), await og.screenshot({ type: 'jpeg', quality: 90 }));
await browser.close();

console.log('og.jpg rebuilt (1200x630)');
