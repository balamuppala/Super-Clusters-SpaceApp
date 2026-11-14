import asyncio, json, sys
from playwright.async_api import async_playwright
URL = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8792/'
OUT = '/home/box/shared/anuspace/build/screenshots/'
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path='/usr/bin/google-chrome', args=['--no-sandbox'])
        pg = await b.new_page(viewport={'width': 1400, 'height': 1000}, accept_downloads=True)
        errs = []; pg.on('pageerror', lambda e: errs.append(str(e))); pg.on('console', lambda m: m.type == 'error' and errs.append(m.text))
        await pg.goto(URL); await pg.wait_for_function('typeof AGG !== "undefined" && AGG && AGG.s', timeout=60000); await pg.wait_for_timeout(3000)
        # consistency: client-side all-France harmonized vs python monthly.json
        chk = await pg.evaluate('''() => { let maxd = 0; MON.forEach(r => { const t = mi(+r.month.slice(0,4), +r.month.slice(5,7)-1);
            for (const [k,a] of [['MODIS',AGG.M],['VIIRS',AGG.V]]) if (r[k] != null) maxd = Math.max(maxd, Math.abs(r[k]-a[t]));
            if (r.HARMONIZED != null) maxd = Math.max(maxd, Math.abs(r.HARMONIZED - AGG.s[t])); }); return {maxd, sel: SEL.length, cur: document.getElementById('current').innerText}; }''')
        print('consistency', chk)
        await pg.screenshot(path=OUT + '01_overview_france.png', full_page=True)
        await pg.select_option('#calMode', 'anom'); await pg.wait_for_timeout(500)
        el = await pg.query_selector('#cal'); await el.screenshot(path=OUT + '02_calendar_anomaly.png')
        await pg.select_option('#calMode', 'count')
        # Lat/lon box AOI: Gironde / Landes (2022 fires), summer 2022
        for k, v in dict(s='44.0', w='-1.6', n='45.0', e='0.0').items(): await pg.fill('#' + k, v)
        await pg.fill('#d0', '2022-07-01'); await pg.fill('#d1', '2022-08-31')
        await pg.click('#applyBox'); await pg.wait_for_timeout(3000)
        await pg.screenshot(path=OUT + '03_aoi_box_gironde_2022.png', full_page=True)
        async with pg.expect_download() as d: await pg.click('#expCsv')
        await pg.wait_for_timeout(1500); print('csv:', await pg.inner_text('#expMsg'))
        async with pg.expect_download() as d: await pg.click('#expGeo')
        await pg.wait_for_timeout(1500); print('geojson:', await pg.inner_text('#expMsg'))
        # drawn polygon AOI via leaflet draw (programmatic click on map) : Var / Provence
        await pg.click('#resetAoi'); await pg.fill('#d0', '2000-11-01'); await pg.fill('#d1', '2026-11-14')
        await pg.wait_for_timeout(1000); print('zoom', await pg.evaluate('(map.setView([43.5, 6.0], 8, {animate:false}), map.getZoom())')); await pg.wait_for_timeout(2000)
        await pg.click('.leaflet-draw-draw-polygon'); mp = await pg.query_selector('#map'); bb = await mp.bounding_box()
        pts = [(0.3, 0.3), (0.7, 0.3), (0.75, 0.7), (0.3, 0.7), (0.3, 0.3)]
        for x, y in pts: await pg.mouse.click(bb['x'] + bb['width']*x, bb['y'] + bb['height']*y); await pg.wait_for_timeout(300)
        await pg.wait_for_timeout(3000); print('poly label:', await pg.inner_text('#searchLabel'), 'sel', await pg.evaluate('SEL.length'))
        await pg.screenshot(path=OUT + '04_aoi_polygon_provence.png', full_page=True)
        print('errors', errs); await b.close()
asyncio.run(main())
