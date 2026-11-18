// Wasalt listing collector. Runs inside a normal Chrome tab on wasalt.sa.
//
// Why in the browser: wasalt.sa sits behind a Cloudflare check that refuses automated browsers
// (headless and Playwright-driven Chrome both fail it, even with a human click). A normal Chrome passes.
//
// Usage:
//   1. Open https://wasalt.sa/en in Chrome.
//   2. Open DevTools (Cmd+Option+J), paste this whole file, press Enter.
//   3. Wait. Progress prints every 50 listings. At the end Chrome downloads wasalt_listings.jsonl.
//      wasaltStop() stops early and downloads what was collected.
//   4. mv ~/Downloads/wasalt_listings.jsonl data/raw/
//
// Source of URLs: the listing sitemap Wasalt publishes in robots.txt.
// Per listing: the page's own data (Next.js data route, same origin), spaced ~1 s apart.
// Kept: property facts and photo URLs with Wasalt's own room label per photo.
// Dropped: owner, agent and broker names, phone numbers, licence numbers.
(async (CONFIG = {sale: 1000, rent: 500, all: false, delayMs: 800, workers: 2}) => {
  const SITEMAP = 'https://cdn.wasalt.sa/sitemap/product_sitemap_en_sa.xml.gz'; // plain XML despite the name
  const IMG = (id, file, w) => `https://imagedelivery.net/1DNKFJPRaeUdy_j8F7HT3w/production/properties/${id}/images/${file}/quality=60,format=auto,w=${w}`;
  const strip = h => (h || '').replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const sample = (arr, n) => {
    const a = arr.slice();
    for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
    return n == null ? a : a.slice(0, n);
  };

  let bid = JSON.parse(document.getElementById('__NEXT_DATA__').textContent).buildId;
  const xml = await (await fetch(SITEMAP)).text();
  const urls = [...xml.matchAll(/<loc>([^<]+)<\/loc>/g)].map(m => m[1]);
  // Sale and rent mixed, so stopping early still leaves both.
  const targets = sample(sample(urls.filter(u => u.includes('/property/sale/')), CONFIG.all ? null : CONFIG.sale)
    .concat(sample(urls.filter(u => u.includes('/property/rent/')), CONFIG.all ? null : CONFIG.rent)));
  console.log(`[wasalt] sitemap ${urls.length} listings, fetching ${targets.length}`);

  const rows = [];
  let stop = false, ok = 0, fail = 0, misses = 0;
  const download = () => {
    const blob = new Blob([rows.map(r => JSON.stringify(r)).join('\n') + '\n'], {type: 'application/x-ndjson'});
    const a = Object.assign(document.createElement('a'), {href: URL.createObjectURL(blob), download: 'wasalt_listings.jsonl'});
    document.body.appendChild(a); a.click(); a.remove();
    console.log(`[wasalt] downloaded ${rows.length} listings`);
  };
  window.wasaltStop = () => { stop = true; };
  window.wasaltRows = rows;

  let next = 0, n = -1;
  const worker = async () => { while (!stop && next < targets.length) { const url = targets[next++]; n++;
    const [, , , , , forType, slugId] = url.split('/');
    try {
      const r = await fetch(`/_next/data/${bid}/en/property/${forType}/${slugId}.json?slug=${forType}&slug-id=${slugId}`);
      if (!r.ok) throw new Error(r.status);
      const pp = (await r.json()).pageProps || {};
      const p = pp.propertyDetailsV3;
      if (!p) throw new Error('no details');
      const i = p.propertyInfo || {};
      const photos = ((pp.galleryDetails && pp.galleryDetails.images && pp.galleryDetails.images.data) || [])
        .filter(x => x.type === 'image' && x.content).slice(0, 12)
        .map(x => ({url: IMG(p.id, x.content, 900), thumb: IMG(p.id, x.content, 400),
                    label: x.classificationName || null, score: x.classificationScore || null}));
      rows.push({
        id: p.id, url, title: i.title, property_for: i.propertyFor, main_type: i.propertyMainType, sub_type: i.propertySubType,
        city: i.city, district: i.district, territory: i.territory, address: i.address,
        sale_price: i.salePrice ? Number(i.salePrice) : null,
        rent: i.rentFreq && typeof i.rentFreq === 'object' ? Object.values(i.rentFreq).map(f => ({freq: f.freq, amount: f.amount})) : null,
        currency: i.currencyType, avg_sale_price_per_sqm: i.averageSalePricePerSqm,
        attributes: (p.attributes || []).map(a => ({key: a.key, name: a.name, value: a.value})),
        details: (p.additionalAttributes || []).filter(a => !['adSource', 'obligations'].includes(a.key))
          .map(a => ({key: a.key, label: a.label, value: a.value})),
        description: strip(i.description).slice(0, 3000),
        lat: p.location && p.location.lat, lon: p.location && p.location.lon,
        published_at: p.publishedAt, is_verified: p.isVerified, photos,
      });
      ok++; misses = 0;
    } catch (e) {
      fail++; misses++;
      if (misses === 5) { // the site redeployed (new build id): read it again from a fresh page load
        const html = await (await fetch('/en')).text();
        const m = html.match(/"buildId":"([^"]+)"/);
        if (m) bid = m[1];
      }
      if (misses >= 20) { console.warn('[wasalt] 20 failures in a row, stopping'); stop = true; }
    }
    if ((n + 1) % 50 === 0) console.log(`[wasalt] ${n + 1}/${targets.length} ok=${ok} fail=${fail}`);
    await sleep(CONFIG.delayMs + Math.random() * 500);
  } };
  await Promise.all(Array.from({length: CONFIG.workers}, worker));
  download();
})();
