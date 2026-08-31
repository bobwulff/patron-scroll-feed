import os
import sys
import json
import base64
import requests

CLIENT_ID = os.environ["PATREON_CLIENT_ID"]
CLIENT_SECRET = os.environ["PATREON_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["PATREON_REFRESH_TOKEN"]
CAMPAIGN_ID = os.environ.get("PATREON_CAMPAIGN_ID", "").strip()
GH_TOKEN = os.environ.get("GH_PAT", "")
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "")

TOKEN_URL = "https://www.patreon.com/api/oauth2/token"
API_BASE = "https://www.patreon.com/api/oauth2/v2"


def api_request(method, url, headers=None, **kwargs):
    resp = requests.request(method, url, headers=headers, **kwargs)
    if not resp.ok:
        print(f"Request failed: {method} {url} -> HTTP {resp.status_code}")
        print(resp.text[:2000])
        resp.raise_for_status()
    return resp


def refresh_access_token():
    resp = api_request("POST", TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    data = resp.json()
    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != REFRESH_TOKEN and GH_TOKEN and GH_REPO:
        rotate_github_secret("PATREON_REFRESH_TOKEN", new_refresh)
    return data["access_token"]


def rotate_github_secret(name, value):
    from nacl import encoding, public
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    key_resp = api_request("GET", f"https://api.github.com/repos/{GH_REPO}/actions/secrets/public-key", headers=headers)
    key_data = key_resp.json()

    public_key = public.PublicKey(key_data["key"].encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted_b64 = base64.b64encode(sealed_box.encrypt(value.encode("utf-8"))).decode("utf-8")

    api_request(
        "PUT",
        f"https://api.github.com/repos/{GH_REPO}/actions/secrets/{name}",
        headers=headers,
        json={"encrypted_value": encrypted_b64, "key_id": key_data["key_id"]},
    )
    print(f"Rotated {name} and saved it back to the repo secret.")


def get_campaign_id(access_token):
    if CAMPAIGN_ID:
        return CAMPAIGN_ID
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = api_request("GET", f"{API_BASE}/campaigns", headers=headers)
    campaigns = resp.json().get("data", [])
    if not campaigns:
        print("No campaigns found for this Patreon account/token.")
        print("This usually means the Patreon client wasn't granted the 'campaigns' scope when it was created.")
        sys.exit(1)
    if len(campaigns) > 1:
        print("Multiple campaigns found - set PATREON_CAMPAIGN_ID to pick one:")
        for c in campaigns:
            print(f"  {c['id']}")
    return campaigns[0]["id"]


def fetch_members(access_token, campaign_id):
    headers = {"Authorization": f"Bearer {access_token}"}
    url = (
        f"{API_BASE}/campaigns/{campaign_id}/members"
        "?include=currently_entitled_tiers,user"
        "&fields[member]=patron_status"
        "&fields[tier]=title"
        "&fields[user]=full_name"
        "&page[count]=200"
    )
    patrons = []
    total_seen = 0
    skipped_status = 0
    skipped_no_tier = 0
    skipped_no_name = 0

    while url:
        resp = api_request("GET", url, headers=headers)
        payload = resp.json()

        tiers_by_id = {i["id"]: i["attributes"].get("title", "") for i in payload.get("included", []) if i["type"] == "tier"}
        users_by_id = {i["id"]: i["attributes"].get("full_name", "") for i in payload.get("included", []) if i["type"] == "user"}

        for member in payload.get("data", []):
            total_seen += 1
            attrs = member["attributes"]
            if attrs.get("patron_status") != "active_patron":
                skipped_status += 1
                continue
            user_rel = member["relationships"].get("user", {}).get("data")
            name = users_by_id.get(user_rel["id"], "") if user_rel else ""
            tier_rels = member["relationships"].get("currently_entitled_tiers", {}).get("data", [])
            tier_name = tiers_by_id.get(tier_rels[0]["id"]) if tier_rels else ""
            if not tier_name:
                skipped_no_tier += 1
                continue
            if not name:
                skipped_no_name += 1
                continue
            patrons.append({"name": name, "tier": tier_name})

        url = payload.get("links", {}).get("next")

    print(f"Saw {total_seen} members total: {len(patrons)} written, {skipped_status} not active_patron, {skipped_no_tier} with no entitled tier, {skipped_no_name} with no resolvable name.")
    return patrons


def main():
    access_token = refresh_access_token()
    campaign_id = get_campaign_id(access_token)
    patrons = fetch_members(access_token, campaign_id)

    if not patrons:
        print("No patrons were written to patrons.json. Common causes:")
        print("- The Patreon client wasn't granted the 'campaigns.members' scope when it was created")
        print("- PATREON_CAMPAIGN_ID doesn't match this campaign (see the log above for the available IDs)")
        print("- None of your patrons currently have patron_status = active_patron")

    with open("patrons.json", "w") as f:
        json.dump(patrons, f, indent=2)
    print(f"Wrote {len(patrons)} active patrons to patrons.json")


if __name__ == "__main__":
    main()
