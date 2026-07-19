# -*- coding: utf-8 -*-
"""Minimal client for Music Assistant's own REST API (POST /api).

Separate from music_assistant_api/ma_routes.py, which is the endpoint MA
pushes stream URLs *to*. This module lets the skill call back *into* MA
to look up in-progress audiobooks/podcasts, search the library (tracks,
artists, albums - across every connected provider, e.g. Jellyfin), and
start playback on a specific player queue.

Auth: a long-lived API token generated in MA's own user settings
(Settings > user profile > API tokens), configured via MA_API_TOKEN.
This is unrelated to APP_USERNAME/APP_PASSWORD (which protect this
skill's own web UI/API) and to the Home Assistant integration's own
system token (which is scoped to MA's WebSocket protocol, not this
REST wrapper).
"""

import logging
import os

import requests

from env_secrets import get_env_secret

logger = logging.getLogger(__name__)


class MAClientError(Exception):
    pass


def _api_url():
    base = (os.environ.get('MA_API_URL') or '').strip().rstrip('/')
    if not base:
        raise MAClientError('MA_API_URL is not configured')
    return f'{base}/api'


def call(command, args=None, timeout=10):
    """Call a Music Assistant API command and return its parsed JSON result."""
    token = get_env_secret('MA_API_TOKEN')
    if not token:
        raise MAClientError('MA_API_TOKEN is not configured')

    body = {'command': command}
    if args:
        body['args'] = args

    try:
        resp = requests.post(
            _api_url(),
            json=body,
            headers={'Authorization': f'Bearer {token}'},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise MAClientError(f'Request to Music Assistant failed: {e}') from e

    if resp.status_code >= 400:
        raise MAClientError(f'Music Assistant API returned HTTP {resp.status_code}: {resp.text[:200]}')

    try:
        return resp.json()
    except ValueError as e:
        raise MAClientError(f'Music Assistant API returned non-JSON response: {resp.text[:200]}') from e


def get_in_progress_items(limit=5):
    """Return the list of in-progress audiobooks/podcast episodes, most recent first."""
    result = call('music/in_progress_items', {'limit': limit})
    return result if isinstance(result, list) else []


def play_media(queue_id, uri):
    """Start playback of the given media URI on the given player queue.

    Short timeout: play_media's return_type is null (fire-and-forget - MA
    queues the command and returns immediately, the actual stream doesn't
    show up until it separately pushes to this skill's /ma/push-url). A
    slow/hanging response here means something's wrong on MA's side, not
    that it's still "working on it" - no reason to make the caller (and
    Alexa's own retry logic) wait a full 10s to find that out.
    """
    call('player_queues/play_media', {'queue_id': queue_id, 'media': uri}, timeout=4)


def list_players():
    """Return all registered MA players (used to populate the device-assignment UI)."""
    result = call('players/all')
    return result if isinstance(result, list) else []


def search(query, media_types=None, limit=5):
    """Search MA's library across all connected providers. Returns the raw
    SearchResults dict (keys: artists, albums, genres, tracks, playlists,
    radio, audiobooks, podcasts), each a list of MediaItems.
    """
    args = {'search_query': query, 'limit': limit}
    if media_types:
        args['media_types'] = media_types
    result = call('music/search', args)
    return result if isinstance(result, dict) else {}


def pick_best_match(results, query=None):
    """Pick a single item to play from search() results.

    MA's search matches the query against any metadata field (title,
    album, etc.), so a plain "first track" pick can return an unrelated
    song that merely contains the query word (e.g. searching "queen"
    returning Madonna's song "Queen" ahead of the actual artist Queen).
    An exact (case-insensitive) name match - most often an artist, since
    a single/short query is usually "play music by X" - is a much
    stronger signal of intent than category order, so it's checked first.
    Falls back to the original track > artist > album order when nothing
    matches exactly. Returns (item, kind) with kind in
    {'track', 'artist', 'album'}, or (None, None) if nothing usable was found.
    """
    query_norm = (query or '').strip().casefold()
    if query_norm:
        for kind in ('artists', 'tracks', 'albums'):
            items = results.get(kind) or []
            for item in items:
                name = (item.get('name') or '').strip().casefold()
                if name == query_norm and item.get('is_playable', True) and item.get('uri'):
                    return item, kind.rstrip('s')

    for kind in ('tracks', 'artists', 'albums'):
        items = results.get(kind) or []
        for item in items:
            if item.get('is_playable', True) and item.get('uri'):
                return item, kind.rstrip('s')
    return None, None
