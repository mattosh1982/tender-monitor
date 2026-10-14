import urllib.request, urllib.parse, json, sys, os, re
UA={"User-Agent":"Mozilla/5.0 (TerradronTenderMonitor probe)"}
out={}
def get(u, n=4000, data=None, h=None):
    try:
        hh=dict(UA); hh.update(h or {})
        r=urllib.request.urlopen(urllib.request.Request(u,data=data,headers=hh),timeout=60)
        b=r.read(); return {"status":r.status,"ctype":r.headers.get("Content-Type"),"len":len(b),"body":b[:n].decode("utf-8","replace")}
    except urllib.error.HTTPError as e:
        return {"status":e.code,"body":e.read()[:800].decode("utf-8","replace")}
    except Exception as e:
        return {"status":0,"err":str(e)}
urls=["https://www.uvo.gov.sk/robots.txt",
"https://www.uvo.gov.sk/vestnik-a-registre/vestnik",
"https://www.uvo.gov.sk/vyhladavanie/vyhladavanie-zakaziek",
"https://data.slovensko.sk/api/action/package_search?q=vestnik",
"https://data.slovensko.sk/api/3/action/package_search?q=vestnik",
"https://data.slovensko.sk/api/sparql",
"https://data.slovensko.sk/sparql",
"https://data.slovensko.sk/robots.txt",
"https://data.slovensko.sk/api/datasets/45a88bf5-0ab4-4a6d-99a2-28e1ff9aa090",
"https://data.slovensko.sk/api/v1/datasets/45a88bf5-0ab4-4a6d-99a2-28e1ff9aa090",
"https://data.slovensko.sk/datasety/45a88bf5-0ab4-4a6d-99a2-28e1ff9aa090",
"https://www.uvo.gov.sk/rss",
"https://www.uvo.gov.sk/vestnik-a-registre/vestnik/rss"]
for u in urls: out[u]=get(u, 6000)
# SPARQL attempt
q="SELECT ?d ?t WHERE { ?d a <http://www.w3.org/ns/dcat#Dataset> ; <http://purl.org/dc/terms/title> ?t . FILTER(CONTAINS(LCASE(STR(?t)),'vestník')) } LIMIT 20"
for ep in ["https://data.slovensko.sk/api/sparql","https://data.slovensko.sk/sparql"]:
    out["SPARQL "+ep]=get(ep+"?query="+urllib.parse.quote(q),6000,h={"Accept":"application/sparql-results+json"})
# links on vestnik page
v=get("https://www.uvo.gov.sk/vestnik-a-registre/vestnik",400000)
out["vestnik_links"]=sorted(set(re.findall(r'href="([^"]+)"',v.get("body",""))))[:300]
s=get("https://www.uvo.gov.sk/vyhladavanie/vyhladavanie-zakaziek",400000)
out["search_links"]=sorted(set(re.findall(r'href="([^"]+)"',s.get("body",""))))[:300]
out["search_forms"]=re.findall(r'<(?:input|select)[^>]+name="([^"]+)"',s.get("body",""))[:100]
os.makedirs("probe",exist_ok=True)
json.dump(out,open("probe/result.json","w"),ensure_ascii=False,indent=1)
