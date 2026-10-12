"""
The name of a place from a position.

A photo taken with a phone carries a latitude and a longitude but no place.
It keeps that position, and its place is named from it, whether or not it
was taken during an eBird checklist: the place and the position of a
checklist are for the photos that carry none. spot() asks Nominatim, the
geocoder of OpenStreetMap (https://nominatim.openstreetmap.org), for the
neighbourhood, the town, the province and the country of the position, in
French. If the position is within EBIRD_KM of one of the photographer's own
eBird places, that name is kept as the place: it is the one the photographer
uses. A position nothing names (the open country, no network) takes the
names of the checklist the photo was taken during, if there is one.

What goes online is the position, rounded to a thousandth of a degree (about
100 m), and nothing else. Each answer is kept in the index (table geocode)
by that cell, so a place is asked once, and the requests are a second apart,
as Nominatim asks. Config 'geocode': false never goes online.
"""

import json
import threading
import time
import urllib.parse
import urllib.request

from . import config as _config_module
from . import photoindex

URL = 'https://nominatim.openstreetmap.org/reverse'
AGENT = 'patchdoc (https://github.com/eartigau/patchnames)'
#: the parts of a Nominatim address that name a place, the finest first
NAMES = ('neighbourhood', 'quarter', 'suburb', 'hamlet', 'village', 'town', 'city_district')
#: and those that name its town
TOWNS = ('city', 'town', 'village', 'municipality', 'hamlet')
#: a position this close to an eBird place of the photographer takes its name
EBIRD_KM = 0.3

_lock, _last = threading.Lock(), [0.0]


class Spot:
    """the place of a photo that carries its position: its names, that
    position, and the checklist the photo was taken during (sid; None
    without one)"""

    def __init__(self, location, county, state, country, lat, lon, sid=None):
        self.location, self.county, self.state, self.country = location, county, state, country
        self.lat, self.lon, self.sid = lat, lon, sid


def reverse(lat, lon):
    """{'location', 'county', 'state', 'country'} of a position: from the
    index if its cell was asked before, from Nominatim otherwise; None when
    it cannot be asked"""
    cell = f'{lat:.3f},{lon:.3f}'
    db = photoindex.connect()
    try:
        row = db.execute('SELECT answer FROM geocode WHERE cell = ?', (cell,)).fetchone()
        if row:
            return json.loads(row['answer'])
        if not _config_module.load().get('geocode', True):
            return None
        query = urllib.parse.urlencode({'lat': cell.split(',')[0], 'lon': cell.split(',')[1],
                                        'format': 'jsonv2', 'accept-language': 'fr', 'zoom': 17,
                                        'addressdetails': 1})
        request = urllib.request.Request(f'{URL}?{query}', headers={'User-Agent': AGENT})
        with _lock:
            time.sleep(max(0.0, 1.1 - (time.time() - _last[0])))
            try:
                with urllib.request.urlopen(request, timeout=20) as reply:
                    address = json.load(reply).get('address') or {}
            except (OSError, ValueError):
                return None
            finally:
                _last[0] = time.time()
        town = next((address[key] for key in TOWNS if address.get(key)), '')
        name = next((address[key] for key in NAMES if address.get(key)), '')
        answer = {'location': f'{name}, {town}' if name and town and name != town else name or town,
                  'county': town or address.get('county', ''),
                  'state': address.get('ISO3166-2-lvl4') or address.get('state', ''),
                  'country': (address.get('country_code') or '').upper()}
        db.execute('INSERT OR REPLACE INTO geocode VALUES (?, ?)',
                   (cell, json.dumps(answer, ensure_ascii=False)))
        db.commit()
        return answer
    finally:
        db.close()


def spot(lat, lon, data=None, during=None):
    """the place of a position, as a Spot: its names from Nominatim, and the
    name of the photographer's eBird place if one is right there (data: the
    eBird checklists); those of `during`, the checklist the photo was taken
    during, when nothing else names it; None when nothing names it"""
    names = reverse(lat, lon)
    near = data.nearest(lat, lon, EBIRD_KM) if data else None
    if near:
        place = near[0]
        names = dict(names or {'county': place.county, 'state': place.state,
                               'country': place.country}, location=place.location)
    if (not names or not names.get('location')) and during:
        names = {'location': during.location, 'county': during.county, 'state': during.state,
                 'country': during.country}
    if not names or not names.get('location'):
        return None
    return Spot(names['location'], names['county'], names['state'], names['country'], lat, lon)
