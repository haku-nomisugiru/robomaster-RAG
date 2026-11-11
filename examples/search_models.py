"""Local AI/tool adapter example. Standard library only."""
import argparse,json
from urllib.parse import urlencode
from urllib.request import urlopen

def search_models(query, section=None, limit=5, mode="auto", category=None):
    params = {"q": query, "limit": limit, "mode": mode}
    if section is not None:
        params["section"] = section
    if category is not None:
        params["category"] = category
    url = "http://127.0.0.1:8765/v1/search?" + urlencode(params)
    with urlopen(url,timeout=30) as response:
        return json.load(response)

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--section",choices=["gimbal","chassis","wheel_or_leg"])
    parser.add_argument("--mode",choices=["auto","function","model"],default="auto")
    parser.add_argument("--category",choices=["chassis","suspension","wheel_leg","swerve","robotic_arm","gripper","gearbox"])
    args=parser.parse_args()
    print(json.dumps(search_models(args.query,args.section,mode=args.mode,category=args.category),ensure_ascii=False,indent=2))
