import urllib.request, json, os, re, time
UA={"User-Agent":"Mozilla/5.0 (TerradronTenderMonitor; +https://terradron.sk)"}
def get(u):
    return urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=60).read().decode("utf-8","replace")
rss=get("https://www.uvo.gov.sk/vestnik-a-registre/vestnik/rss")
items=re.findall(r"<item><title>(.*?)</title>.*?<link>(.*?)</link>",rss)
pick=[i for i in items if "vyhlásení" in i[0] and "Oprava" not in i[0]][:2]+[i for i in items if "Výzva" in i[0]][:1]
os.makedirs("probe/html",exist_ok=True)
for n,(t,l) in enumerate(pick):
    open(f"probe/html/{n}.html","w").write(f"<!-- {t} | {l} -->\n"+get(l)); time.sleep(3)
