# আমার ২০২৬ – Year Recap

সাধারণ মানুষের জন্য বাংলা বছরের রিক্যাপ সাইট। কয়েকটা প্রশ্নের উত্তর → AI দিয়ে বাংলায় গল্প → নিজের ছবি দিয়ে শেয়ার কার্ড (স্টোরি ৯:১৬ বা ফিড পোস্ট ৪:৫)।

## ফাইল

- `index.html` – পুরো সাইট (একটাই ফাইল, কোনো build লাগে না)
- `api/recap.js` – Vercel serverless function, AI দিয়ে গল্প লেখে
- `package.json`, `.gitignore`

## Vercel-এ deploy

1. এই ফোল্ডারটা একটা GitHub repo-তে push করো (private রাখলেও চলবে)।
2. vercel.com → **Add New → Project** → repo টা import করো। Framework Preset: **Other**। Build command / output খালি রাখো।
3. **Settings → Environment Variables**-এ যোগ করো:
   - `ANTHROPIC_API_KEY` = তোমার Anthropic API key (console.anthropic.com থেকে)
   - (ঐচ্ছিক) `ANTHROPIC_MODEL` = অন্য মডেল চাইলে, যেমন `claude-sonnet-5-5`। না দিলে `claude-haiku-4-5-20251001` চলবে (সস্তা, দ্রুত)।
4. **Deploy** চাপো। Env variable পরে যোগ করলে আবার Redeploy করতে হবে।

CLI দিয়েও করা যায়:
```bash
npm i -g vercel
vercel            # প্রথমবার link করবে
vercel env add ANTHROPIC_API_KEY
vercel --prod
```

লোকালি টেস্ট: `vercel dev` চালিয়ে http://localhost:3000 খোলো।

## খরচ আর নিরাপত্তা

- প্রতিটা "গল্প বানাও" = একটা AI কল। খরচ তোমার API account থেকে যাবে। console.anthropic.com-এ মাসিক spend limit সেট করে রাখো।
- `api/recap.js`-এ প্রতি IP-তে ১০ মিনিটে ৬টা রিকোয়েস্টের একটা সাধারণ লিমিট আছে, কিন্তু serverless-এ এটা নিখুঁত না। ভাইরাল হলে Vercel Firewall-এ rate limit rule দাও।
- API key শুধু সার্ভারে থাকে, ব্রাউজারে যায় না।
- উত্তরগুলো কোথাও লগ বা জমা হয় না। ছবি ব্যবহারকারীর ফোনেই থাকে, কোথাও আপলোড হয় না।
- AI কাজ না করলে (key নেই, লিমিট, এরর) সাইট নিজে থেকেই সাধারণ টেমপ্লেট দিয়ে রিক্যাপ বানিয়ে দেয়, কিছু ভাঙে না।

## পরে যা যোগ করা যায়

- `og:image` – লিংক শেয়ার করলে প্রিভিউ ছবি (একটা 1200×630 ছবি বানিয়ে `og.png` নামে রেখে `<head>`-এ যোগ করো)
- নিজের ডোমেইন: Vercel → Settings → Domains
- Analytics: Vercel Analytics অন করো (কয়জন কার্ড বানাল দেখতে)
