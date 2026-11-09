"""Local AI/tool adapter example. Standard library only."""
import argparse,json
from urllib.parse import urlencode
from urllib.request import urlopen

def search_models(query, section, limit=5):
    url="http://127.0.0.1:8765/v1/search?"+urlencode({"q":query,"section":section,"limit":limit})
    with urlopen(url,timeout=30) as response:
        return json.load(response)

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--section",required=True,choices=["gimbal","chassis","wheel_or_leg"])
    args=parser.parse_args()
    print(json.dumps(search_models(args.query,args.section),ensure_ascii=False,indent=2))
