"""War Room sync robot (runs on GitHub Actions; written Nov 12, 2026).

Every run it saves, into data/:
  leagues.json      your ESPN leagues: settings (scoring, lineup slots, playoff format), every team (name, record, points),
                    every roster (player, ESPN slot, injury status) and the full matchup schedule with scores
  espn_players.json every active ESPN fantasy player: id, name, position, NFL team, injury status
  heads.json        a small 96x96 photo for every QB/RB/WR/TE/K (rookies included), refreshed every 14 days
  status.json       when it ran and whether each league worked (an error here usually means the cookies expired)
The War Room build reads these files from raw.githubusercontent.com. Nothing secret is written: the ESPN cookies stay in
GitHub Secrets (ESPN_S2, ESPN_SWID) and are never printed.
"""
import base64, datetime as dt, io, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
import requests
from PIL import Image

CFG = json.load(open('config.json'))
SEASON = int(CFG.get('season', 2026))
BASE = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{}'.format(SEASON)
COOKIES = {k: v for k, v in (('espn_s2', os.environ.get('ESPN_S2', '').strip()), ('SWID', os.environ.get('ESPN_SWID', '').strip())) if v}
HDR = {'User-Agent': 'Mozilla/5.0 (war-room-sync)', 'Accept': 'application/json'}
TEAM = {0: 'FA', 1: 'ATL', 2: 'BUF', 3: 'CHI', 4: 'CIN', 5: 'CLE', 6: 'DAL', 7: 'DEN', 8: 'DET', 9: 'GB', 10: 'TEN', 11: 'IND', 12: 'KC',
        13: 'LV', 14: 'LA', 15: 'MIA', 16: 'MIN', 17: 'NE', 18: 'NO', 19: 'NYG', 20: 'NYJ', 21: 'PHI', 22: 'ARI', 23: 'PIT', 24: 'LAC',
        25: 'SF', 26: 'SEA', 27: 'TB', 28: 'WAS', 29: 'CAR', 30: 'JAX', 33: 'BAL', 34: 'HOU'}
POS = {1: 'QB', 2: 'RB', 3: 'WR', 4: 'TE', 5: 'K', 16: 'DST'}
SLOT = {0: 'QB', 2: 'RB', 3: 'RB/WR', 4: 'WR', 5: 'WR/TE', 6: 'TE', 7: 'OP', 16: 'DST', 17: 'K', 20: 'BN', 21: 'IR', 23: 'FLEX', 24: 'ER'}
os.makedirs('data', exist_ok=True)
NOW = dt.datetime.now(dt.timezone.utc)


def get(url, params=None, headers=None, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers={**HDR, **(headers or {})}, cookies=COOKIES, timeout=40)
            if r.status_code == 200: return r
            if r.status_code in (401, 403): raise RuntimeError(f'ESPN said {r.status_code}: private league and the cookies are missing or expired')
            if r.status_code == 404: raise RuntimeError('ESPN said 404: check the league id in config.json')
        except requests.RequestException as e:
            if i == tries - 1: raise RuntimeError(f'network error: {e}')
        time.sleep(2 * (i + 1))
    raise RuntimeError(f'ESPN kept failing ({r.status_code})')


def team_name(t):
    return t.get('name') or ' '.join(x for x in (t.get('location'), t.get('nickname')) if x) or t.get('abbrev') or f"Team {t.get('id')}"


def league(lid):
    views = ['mTeam', 'mRoster', 'mSettings', 'mMatchupScore', 'mStatus']
    raw = get(f'{BASE}/segments/0/leagues/{lid}', params=[('view', v) for v in views]).json()
    if os.environ.get('SAVE_RAW') == 'true':   # manual runs keep one raw copy for debugging the parser
        os.makedirs('data/raw', exist_ok=True); json.dump(raw, open(f'data/raw/{lid}.json', 'w'))
    st, sc, rs = raw.get('settings', {}), raw.get('settings', {}).get('scheduleSettings', {}), raw.get('settings', {}).get('rosterSettings', {})
    teams = []
    for t in raw.get('teams', []):
        rec = (t.get('record') or {}).get('overall', {})
        roster = []
        for e in (t.get('roster') or {}).get('entries', []):
            p = (e.get('playerPoolEntry') or {}).get('player') or {}
            roster.append(dict(espn_id=e.get('playerId'), name=p.get('fullName'), pos=POS.get(p.get('defaultPositionId')), team=TEAM.get(p.get('proTeamId'), ''),
                               slot=SLOT.get(e.get('lineupSlotId'), str(e.get('lineupSlotId'))), injury=p.get('injuryStatus') or e.get('injuryStatus')))
        teams.append(dict(id=t.get('id'), name=team_name(t), abbrev=t.get('abbrev'), wins=rec.get('wins'), losses=rec.get('losses'), ties=rec.get('ties'),
                          points_for=rec.get('pointsFor'), points_against=rec.get('pointsAgainst'), roster=roster))
    sched = [dict(week=m.get('matchupPeriodId'), home=(m.get('home') or {}).get('teamId'), away=(m.get('away') or {}).get('teamId'),
                  home_pts=(m.get('home') or {}).get('totalPoints'), away_pts=(m.get('away') or {}).get('totalPoints'), winner=m.get('winner'))
             for m in raw.get('schedule', [])]
    scoring = [dict(stat=s.get('statId'), points=s.get('points')) for s in (st.get('scoringSettings') or {}).get('scoringItems', [])]
    return dict(id=lid, name=st.get('name'), current_week=(raw.get('status') or {}).get('currentMatchupPeriod') or raw.get('scoringPeriodId'),
                regular_season_weeks=sc.get('matchupPeriodCount'), playoff_teams=sc.get('playoffTeamCount'),
                lineup=({SLOT.get(int(k), k): v for k, v in (rs.get('lineupSlotCounts') or {}).items() if v}),
                ppr=next((s['points'] for s in scoring if s['stat'] == 53), None), scoring=scoring, teams=teams, schedule=sched)


def players():
    flt = {'filterActive': {'value': True}}
    data = get(f'{BASE}/players', params={'scoringPeriodId': 0, 'view': 'players_wl'}, headers={'x-fantasy-filter': json.dumps(flt)}).json()
    out = []
    for p in data:
        pos = POS.get(p.get('defaultPositionId'))
        if pos: out.append(dict(id=p['id'], name=p.get('fullName'), pos=pos, team=TEAM.get(p.get('proTeamId'), ''), injury=p.get('injuryStatus')))
    return out


def photo(pid):
    try:
        r = requests.get(f'https://a.espncdn.com/i/headshots/nfl/players/full/{pid}.png', headers=HDR, timeout=30)
        if r.status_code != 200 or len(r.content) < 2000: return None
        im = Image.open(io.BytesIO(r.content)).convert('RGBA')
        bb = im.getbbox() or (0, 0) + im.size
        w, h = bb[2] - bb[0], bb[3] - bb[1]; side = int(min(h * 0.9, w)); cx = (bb[0] + bb[2]) // 2
        im = im.crop((cx - side // 2, bb[1], cx - side // 2 + side, bb[1] + side)).resize((96, 96), Image.LANCZOS)
        buf = io.BytesIO(); im.save(buf, 'WEBP', quality=72, method=6)
        return 'data:image/webp;base64,' + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def main():
    status = dict(ran=NOW.isoformat(timespec='seconds'), season=SEASON, cookies=bool(COOKIES), leagues={}, players=None, photos=None)
    out = []
    for L in CFG.get('leagues', []):
        lid = str(L.get('id', '')).strip()
        if not lid.isdigit(): status['leagues'][L.get('name', '?')] = 'skipped: no league id in config.json'; continue
        try:
            x = league(lid); x['my_team_name'] = L.get('my_team'); out.append(x)
            status['leagues'][L.get('name') or lid] = f"ok: {len(x['teams'])} teams, week {x['current_week']}"
        except Exception as e:
            status['leagues'][L.get('name') or lid] = f'ERROR {e}'
    if out: json.dump(dict(updated=status['ran'], leagues=out), open('data/leagues.json', 'w'), indent=1, sort_keys=True)
    try:
        pl = players(); json.dump(pl, open('data/espn_players.json', 'w'), separators=(',', ':'), sort_keys=True)
        status['players'] = len(pl)
        H = json.load(open('data/heads.json')) if os.path.exists('data/heads.json') else {}
        cutoff = (NOW - dt.timedelta(days=14)).date().isoformat()
        todo = [p for p in pl if p['pos'] in ('QB', 'RB', 'WR', 'TE', 'K') and (str(p['id']) not in H or H[str(p['id'])].get('d', '') < cutoff)]
        with ThreadPoolExecutor(12) as ex:
            for p, img in zip(todo, ex.map(lambda q: photo(q['id']), todo)):
                if img: H[str(p['id'])] = dict(n=p['name'], p=p['pos'], t=p['team'], img=img, d=NOW.date().isoformat())
        json.dump(H, open('data/heads.json', 'w'), separators=(',', ':'), sort_keys=True)
        status['photos'] = f'{len(H)} saved ({len(todo)} checked this run)'
    except Exception as e:
        status['players'] = f'ERROR {e}'
    json.dump(status, open('data/status.json', 'w'), indent=1)
    print(json.dumps(status, indent=1))
    if not out and CFG.get('leagues'): sys.exit('No league could be read: see data/status.json')


if __name__ == '__main__':
    main()
