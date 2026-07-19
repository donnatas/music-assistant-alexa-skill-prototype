# -*- coding: utf-8 -*-

import logging
import gettext
import os
import time
import requests
from ask_sdk.standard import StandardSkillBuilder
from ask_sdk_core.dispatch_components import (
    AbstractRequestHandler, AbstractExceptionHandler,
    AbstractRequestInterceptor, AbstractResponseInterceptor)
from ask_sdk_core.utils import is_request_type, is_intent_name
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_model import Response
from env_secrets import get_env_secret

from . import data, util, ma_client, device_registry

sb = StandardSkillBuilder()
# sb = StandardSkillBuilder(
#     table_name=data.jingle["db_table"], auto_create_table=True)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class _ComponentFilter(logging.Filter):
    """Inject a `component` attribute based on logger name.

    This makes it easy to tell whether a message came from the
    API, the Alexa Skill code (Skill), or the UI/Web app.
    """
    def filter(self, record):
        name = (record.name or "")
        path = (getattr(record, 'pathname', '') or '')
        norm_path = path.replace(os.sep, '/') if path else ''
        if name.startswith('music_assistant_api') or name.startswith('ma_routes'):
            record.component = 'API'
        elif name.startswith('alexa') or name == 'lambda_function' or name.startswith('ask_sdk'):
            record.component = 'Skill'
        elif norm_path:
            if "/app/skill/" in norm_path:
                record.component = 'Skill'
            elif "/app/music_assistant_api/" in norm_path or "/app/alexa_api/" in norm_path:
                record.component = 'API'
            elif "/app/endpoints/" in norm_path or norm_path.endswith("/app.py"):
                record.component = 'UI/Web'
            else:
                record.component = 'UI/Web'
        else:
            record.component = 'UI/Web'
        return True


_filter = _ComponentFilter()
root_logger = logging.getLogger()
root_logger.addFilter(_filter)

# Ensure every LogRecord has a `component` attribute so formatters
# that reference %(component)s don't fail for third-party loggers
# (e.g. werkzeug) which may emit records before filters run.
_orig_log_record_factory = logging.getLogRecordFactory()

def _log_record_factory(*args, **kwargs):
    record = _orig_log_record_factory(*args, **kwargs)
    if not hasattr(record, 'component'):
        name = (getattr(record, 'name', '') or '')
        path = (getattr(record, 'pathname', '') or '')
        norm_path = path.replace(os.sep, '/') if path else ''
        if name.startswith('music_assistant_api') or name.startswith('ma_routes'):
            record.component = 'API'
        elif name.startswith('alexa') or name == 'lambda_function' or name.startswith('ask_sdk'):
            record.component = 'Skill'
        elif norm_path:
            if "/app/skill/" in norm_path:
                record.component = 'Skill'
            elif "/app/music_assistant_api/" in norm_path or "/app/alexa_api/" in norm_path:
                record.component = 'API'
            elif "/app/endpoints/" in norm_path or norm_path.endswith("/app.py"):
                record.component = 'UI/Web'
            else:
                record.component = 'UI/Web'
        else:
            record.component = 'UI/Web'
    return record

logging.setLogRecordFactory(_log_record_factory)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(component)s] %(name)s %(message)s",
    datefmt="%H:%M:%S %Y-%m-%d %z"
)

supports_apl = False

def _get_stream_url(request):
    """Return (url, audio_data) where url is resolved from util.audio_data.

    Handles multiple shapes returned by util.audio_data and never raises.
    """
    try:
        audio = util.audio_data(request)
    except Exception:
        audio = None

    url = None
    if isinstance(audio, dict):
        url = (audio.get('url') or audio.get('audioSources') or
               audio.get('audio_sources') or audio.get('stream') or '')
    elif isinstance(audio, str):
        url = audio

    if url == '':
        url = None
    return url, audio

# ######################### INTENT HANDLERS #########################
# This section contains handlers for the built-in intents and generic
# request handlers like launch, session end, skill events etc.

class CheckAudioInterfaceHandler(AbstractRequestHandler):
    """Check if device supports audio play.

    This can be used as the first handler to be checked, before invoking
    other handlers, thus making the skill respond to unsupported devices
    without doing much processing.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        if (handler_input.request_envelope.context and 
            handler_input.request_envelope.context.system and 
            handler_input.request_envelope.context.system.device and
            handler_input.request_envelope.context.system.device.supported_interfaces):
            # Since skill events won't have device information
            return handler_input.request_envelope.context.system.device.supported_interfaces.audio_player is None
        else:
            return False

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In CheckAudioInterfaceHandler")
        _ = handler_input.attributes_manager.request_attributes["_"]
        handler_input.response_builder.speak(
            _(data.DEVICE_NOT_SUPPORTED)).set_should_end_session(True)
        return handler_input.response_builder.response


class SkillEventHandler(AbstractRequestHandler):
    """Close session for skill events or when session ends.

    Handler to handle session end or skill events (SkillEnabled,
    SkillDisabled etc.)
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return (handler_input.request_envelope.request.object_type.startswith(
            "AlexaSkillEvent") or
                is_request_type("SessionEndedRequest")(handler_input))

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In SkillEventHandler")
        return handler_input.response_builder.response


class LaunchRequestOrPlayAudioHandler(AbstractRequestHandler):
    """Launch radio for skill launch or PlayAudio intent."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return (is_request_type("LaunchRequest")(handler_input) or
                is_intent_name("PlayAudio")(handler_input))

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In LaunchRequestOrPlayAudioHandler")

        _ = handler_input.attributes_manager.request_attributes["_"]
        request = handler_input.request_envelope.request
        url, _audio = _get_stream_url(request)
        if not url:
            logger.warning("No streamUrl available for Launch/Play request")
            handler_input.response_builder.speak(
                "Sorry, I could not retrieve the latest music stream from the API. Please check your setup.").set_should_end_session(True)
            return handler_input.response_builder.response

        return util.play(
            url=url,
            offset=0,
            text=data.WELCOME_MSG,
            response_builder=handler_input.response_builder,
            supports_apl=supports_apl
        )


def _poll_for_new_stream_url(before_version, timeout=6, interval=0.5):
    """Poll our own /ma/latest-url until its version changes from before_version.

    Deliberately bypasses data.get_latest()'s module-global _last_version/info
    cache - that state is shared with every other handler in this process, and
    piggybacking on it here would race with unrelated requests. Returns the raw
    (unrewritten) streamUrl, or None on timeout.
    """
    port = os.environ.get('PORT')
    url = f"http://127.0.0.1:{port}/ma/latest-url"
    auth = None
    user = get_env_secret('APP_USERNAME')
    pwd = get_env_secret('APP_PASSWORD')
    if user and pwd:
        auth = (user, pwd)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = requests.get(url, auth=auth, timeout=3)
            if resp.status_code == 200:
                payload = resp.json()
                if payload.get('version') != before_version and payload.get('streamUrl'):
                    return payload['streamUrl']
        except requests.RequestException:
            logger.exception('Polling %s failed', url)
        time.sleep(interval)
    return None


def _resolve_player_for_device(handler_input):
    """Record the requesting device as seen and return its mapped MA player_id (or None)."""
    device_id = None
    try:
        device_id = handler_input.request_envelope.context.system.device.device_id
    except Exception:
        logger.exception("Could not read device_id from request")

    device_registry.record_seen(device_id)
    return device_registry.get_player_id(device_id)


def _not_set_up_response(handler_input):
    handler_input.response_builder.speak(
        "Dieses Gerät ist noch nicht eingerichtet. Bitte weise es auf der Setup-Seite "
        "einem Music-Assistant-Player zu."
    ).set_should_end_session(True)
    return handler_input.response_builder.response


def _start_ma_playback_and_respond(handler_input, player_id, uri, success_text):
    """Ask MA to play uri on player_id, wait for the resulting push, then respond.

    Shared by every intent that starts playback via Music Assistant
    (continue-audiobook, search-and-play, ...): call player_queues/play_media,
    poll our own /ma/latest-url for the stream URL MA's "alexa" player
    provider pushes back as a side effect, then hand it to util.play().
    """
    before_version = None
    try:
        baseline = requests.get(
            f"http://127.0.0.1:{os.environ.get('PORT')}/ma/latest-url",
            auth=(get_env_secret('APP_USERNAME'), get_env_secret('APP_PASSWORD')),
            timeout=3,
        )
        if baseline.status_code == 200:
            before_version = baseline.json().get('version')
    except requests.RequestException:
        pass

    try:
        ma_client.play_media(queue_id=player_id, uri=uri)
    except ma_client.MAClientError:
        logger.exception("Failed to start playback via Music Assistant")
        handler_input.response_builder.speak(
            "Music Assistant konnte die Wiedergabe nicht starten."
        ).set_should_end_session(True)
        return handler_input.response_builder.response

    url = _poll_for_new_stream_url(before_version)
    if not url:
        logger.warning("Timed out waiting for Music Assistant to push a stream URL")
        handler_input.response_builder.speak(
            "Music Assistant hat die Wiedergabe gestartet, aber ich konnte die Stream-URL "
            "nicht rechtzeitig abrufen."
        ).set_should_end_session(True)
        return handler_input.response_builder.response

    return util.play(
        url=url,
        offset=0,
        text=success_text,
        response_builder=handler_input.response_builder,
        supports_apl=supports_apl
    )


class ContinueAudiobookIntentHandler(AbstractRequestHandler):
    """Resume the most recently in-progress audiobook/podcast episode.

    Looks up "continue listening" from Music Assistant (which reflects
    Audiobookshelf's own server-side progress, so this works regardless of
    which app was used to listen - MA's own UI, AabsPlayer, anything synced
    to the same Audiobookshelf account) and starts it on whichever MA player
    is mapped to the Echo that asked, via device_registry.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return is_intent_name("ContinueAudiobookIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In ContinueAudiobookIntentHandler")

        player_id = _resolve_player_for_device(handler_input)
        if not player_id:
            logger.warning("No Music Assistant player mapped for this device")
            return _not_set_up_response(handler_input)

        try:
            items = ma_client.get_in_progress_items(limit=1)
        except ma_client.MAClientError:
            logger.exception("Failed to fetch in-progress items from Music Assistant")
            handler_input.response_builder.speak(
                "Ich konnte Music Assistant nicht nach deinem letzten Hörbuch fragen."
            ).set_should_end_session(True)
            return handler_input.response_builder.response

        if not items:
            handler_input.response_builder.speak(
                "Ich habe kein Hörbuch gefunden, das du gerade hörst."
            ).set_should_end_session(True)
            return handler_input.response_builder.response

        item = items[0]
        title = item.get('name') or ''
        text = f"Ich mache weiter mit {title}." if title else "Ich mache weiter."

        return _start_ma_playback_and_respond(handler_input, player_id, item.get('uri'), text)


class PlaySearchIntentHandler(AbstractRequestHandler):
    """Search Music Assistant's library (tracks, artists, albums - across all
    connected providers, e.g. Jellyfin) for a free-text query and play the
    best match on whichever MA player is mapped to the Echo that asked.

    Prefers an exact name match (most often an artist - "spiele Queen") over
    a same-word-in-the-title track ("spiele Bohemian Rhapsody"), then falls
    back to the first playable track/artist/album - matching how people
    naturally ask for either a specific song or "some music by X".
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return is_intent_name("PlaySearchIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In PlaySearchIntentHandler")

        player_id = _resolve_player_for_device(handler_input)
        if not player_id:
            logger.warning("No Music Assistant player mapped for this device")
            return _not_set_up_response(handler_input)

        slots = getattr(handler_input.request_envelope.request.intent, 'slots', None) or {}
        query_slot = slots.get('SearchQuery')
        query = getattr(query_slot, 'value', None) if query_slot else None

        if not query:
            handler_input.response_builder.speak(
                "Ich habe nicht verstanden, was ich spielen soll."
            ).set_should_end_session(True)
            return handler_input.response_builder.response

        try:
            results = ma_client.search(query)
        except ma_client.MAClientError:
            logger.exception("Failed to search Music Assistant for %r", query)
            handler_input.response_builder.speak(
                "Ich konnte Music Assistant nicht durchsuchen."
            ).set_should_end_session(True)
            return handler_input.response_builder.response

        item, kind = ma_client.pick_best_match(results, query)
        if not item:
            handler_input.response_builder.speak(
                f"Ich habe nichts zu \"{query}\" gefunden."
            ).set_should_end_session(True)
            return handler_input.response_builder.response

        name = item.get('name') or query
        if kind == 'artist':
            text = f"Ich spiele Musik von {name}."
        elif kind == 'album':
            text = f"Ich spiele das Album {name}."
        else:
            artists = item.get('artists') or []
            artist_name = artists[0].get('name') if artists and isinstance(artists[0], dict) else None
            text = f"Ich spiele {name} von {artist_name}." if artist_name else f"Ich spiele {name}."

        return _start_ma_playback_and_respond(handler_input, player_id, item.get('uri'), text)


class HelpIntentHandler(AbstractRequestHandler):
    """Handler for providing help information to user."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In HelpIntentHandler")
        _ = handler_input.attributes_manager.request_attributes["_"]
        handler_input.response_builder.speak(
            _(data.HELP_MSG).format(
                util.audio_data(
                    handler_input.request_envelope.request))
        ).set_should_end_session(False)
        return handler_input.response_builder.response


class UnhandledIntentHandler(AbstractRequestHandler):
    """Handler for fallback intent, for unmatched utterances.

    2018-July-12: AMAZON.FallbackIntent is currently available in all
    English locales. This handler will not be triggered except in that
    locale, so it can be safely deployed for any locale. More info
    on the fallback intent can be found here:
    https://developer.amazon.com/docs/custom-skills/standard-built-in-intents.html#fallback
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return is_intent_name("AMAZON.FallbackIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In UnhandledIntentHandler")
        _ = handler_input.attributes_manager.request_attributes["_"]
        handler_input.response_builder.speak(
            _(data.UNHANDLED_MSG)).set_should_end_session(True)
        return handler_input.response_builder.response


class NextOrPreviousIntentHandler(AbstractRequestHandler):
    """Handler for next or previous intents."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return (is_intent_name("AMAZON.NextIntent")(handler_input) or
                is_intent_name("AMAZON.PreviousIntent")(handler_input))

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In NextOrPreviousIntentHandler")
        _ = handler_input.attributes_manager.request_attributes["_"]
        handler_input.response_builder.speak(
            _(data.CANNOT_SKIP_MSG)).set_should_end_session(True)
        return handler_input.response_builder.response


class CancelOrStopIntentHandler(AbstractRequestHandler):
    """Handler for cancel and stop intents."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return (is_intent_name("AMAZON.CancelIntent")(handler_input) or
                is_intent_name("AMAZON.StopIntent")(handler_input))

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In CancelOrStopIntentHandler")
        _ = handler_input.attributes_manager.request_attributes["_"]
        return util.stop(_(data.STOP_MSG), handler_input.response_builder, supports_apl=supports_apl)


class PauseIntentHandler(AbstractRequestHandler):
    """Handler for AMAZON.PauseIntent."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return is_intent_name("AMAZON.PauseIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In PauseIntentHandler")
        _ = handler_input.attributes_manager.request_attributes["_"]
        session_new = False
        if getattr(handler_input.request_envelope, 'session', None):
            session_new = bool(handler_input.request_envelope.session.new)

        return util.pause(text=None,
                  response_builder=handler_input.response_builder,
                  supports_apl=supports_apl,
                  session_new=session_new)


class ResumeIntentHandler(AbstractRequestHandler):
    """Handler for resume intent."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return is_intent_name("AMAZON.ResumeIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In ResumeIntentHandler")
        request = handler_input.request_envelope.request
        _ = handler_input.attributes_manager.request_attributes["_"]
        url, _audio = _get_stream_url(request)
        if not url:
            logger.warning("No stream url available for Resume request")
            handler_input.response_builder.speak(
                "Sorry, I couldn't reach the stream right now.").set_should_end_session(True)
            return handler_input.response_builder.response

        return util.play(
            url=url, 
            offset=0,
            text=data.WELCOME_MSG,
            response_builder=handler_input.response_builder,
            supports_apl=supports_apl
        )


class StartOverIntentHandler(AbstractRequestHandler):
    """Handler for start over, loop on/off, shuffle on/off intent."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return (is_intent_name("AMAZON.StartOverIntent")(handler_input) or
                is_intent_name("AMAZON.LoopOnIntent")(handler_input) or
                is_intent_name("AMAZON.LoopOffIntent")(handler_input) or
                is_intent_name("AMAZON.ShuffleOnIntent")(handler_input) or
                is_intent_name("AMAZON.ShuffleOffIntent")(handler_input))

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In StartOverIntentHandler")

        _ = handler_input.attributes_manager.request_attributes["_"]
        speech = _(data.NOT_POSSIBLE_MSG)
        return handler_input.response_builder.speak(speech).response

# ###################################################################

# ########## AUDIOPLAYER INTERFACE HANDLERS #########################
# This section contains handlers related to Audioplayer interface

class PlaybackStartedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackStarted Directive received.

    Confirming that the requested audio file began playing.
    Do not send any specific response.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return is_request_type("AudioPlayer.PlaybackStarted")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In PlaybackStartedHandler")
        logger.info("Playback started")
        return handler_input.response_builder.response

class PlaybackFinishedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackFinished Directive received.

    Confirming that the requested audio file completed playing.
    Do not send any specific response.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return is_request_type("AudioPlayer.PlaybackFinished")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In PlaybackFinishedHandler")
        logger.info("Playback finished")
        return handler_input.response_builder.response


class PlaybackStoppedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackStopped Directive received.

    Confirming that the requested audio file stopped playing.
    Do not send any specific response.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return is_request_type("AudioPlayer.PlaybackStopped")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In PlaybackStoppedHandler")
        logger.info("Playback stopped")
        return handler_input.response_builder.response


class PlaybackNearlyFinishedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackNearlyFinished Directive received.

    Replacing queue with the URL again. This should not happen on live streams.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return is_request_type("AudioPlayer.PlaybackNearlyFinished")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In PlaybackNearlyFinishedHandler")
        logger.info("Playback nearly finished")
        request = handler_input.request_envelope.request
        url, _audio = _get_stream_url(request)
        if not url:
            logger.warning("No stream url available for PlaybackNearlyFinished")
            return handler_input.response_builder.response

        return util.play_later(
            url=url,
            response_builder=handler_input.response_builder
        )


class PlaybackFailedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackFailed Directive received.

    Logging the error and restarting playing with no output speech and card.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return is_request_type("AudioPlayer.PlaybackFailed")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In PlaybackFailedHandler")
        request = handler_input.request_envelope.request
        logger.info("Playback failed: {}".format(request.error))
        url, _audio = _get_stream_url(request)
        if not url:
            logger.warning("No stream url available for PlaybackFailed; skipping restart")
            return handler_input.response_builder.response

        return util.play(
            url=url, 
            offset=0, 
            text=None,
            response_builder=handler_input.response_builder,
            supports_apl=supports_apl
        )


class ExceptionEncounteredHandler(AbstractRequestHandler):
    """Handler to handle exceptions from responses sent by AudioPlayer
    request.
    """
    def can_handle(self, handler_input):
        # type; (HandlerInput) -> bool
        return is_request_type("System.ExceptionEncountered")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("\n**************** EXCEPTION *******************")
        logger.info(handler_input.request_envelope)
        return handler_input.response_builder.response

# ###################################################################

# ########## APL INTERFACE HANDLERS #################################
# This section contains handlers related to APL interface

class APLUserEventHandler(AbstractRequestHandler):
    """Handler for APL UserEvent requests.
    
    This handles periodic metadata refresh events sent from the APL document.
    When the APL display sends a UserEvent with eventType='MetadataRefresh',
    this handler fetches the latest metadata from Music Assistant and sends
    an updated APL document to refresh the display.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        if not is_request_type("Alexa.Presentation.APL.UserEvent")(handler_input):
            return False
        
        # Check if this is a metadata refresh event
        request = handler_input.request_envelope.request
        try:
            arguments = getattr(request, 'arguments', [])
            if arguments and len(arguments) > 0:
                event_type = arguments[0]
                return event_type == 'MetadataRefresh'
        except Exception:
            pass
        return False

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        
        # Fetch latest metadata from Music Assistant
        changed = False
        try:
            result = data.get_latest()
            changed = bool(result and result.get('changed'))
            if changed:
                logger.info("Metadata changed")
            else:
                logger.debug("Metadata unchanged, skipping update")
        except Exception:
            logger.exception("Failed to fetch latest metadata")
        
        # Check if we have valid metadata
        if not data.info.get('audioSources'):
            logger.warning("No audio sources available for metadata refresh")
        else:
            # Send updated APL document with new metadata
            if changed:
                try:
                    util.update_apl_metadata(handler_input.response_builder)
                    logger.info("APL metadata update directive added to response")
                except Exception:
                    logger.exception("Failed to update APL metadata")
        
        # Always schedule the next refresh so polling continues.
        try:
            util.schedule_apl_refresh(handler_input.response_builder)
        except Exception:
            logger.exception("Failed to schedule APL refresh")
        
        # Explicitly keep session open to allow continued UserEvents
        return handler_input.response_builder.set_should_end_session(False).response

# ###################################################################

# ########## PLAYBACK CONTROLLER INTERFACE HANDLERS #################
# This section contains handlers related to Playback Controller interface
# https://developer.amazon.com/docs/custom-skills/playback-controller-interface-reference.html#requests

class PlayCommandHandler(AbstractRequestHandler):
    """Handler for Play command from hardware buttons or touch control.

    This handler handles the play command sent through hardware buttons such
    as remote control or the play control from Alexa-devices with a screen.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return is_request_type(
            "PlaybackController.PlayCommandIssued")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In PlayCommandHandler")
        _ = handler_input.attributes_manager.request_attributes["_"]
        request = handler_input.request_envelope.request
        url, _audio = _get_stream_url(request)
        if not url:
            logger.warning("No stream url available for PlayCommand; notifying user")
            handler_input.response_builder.speak(
                "Sorry, I couldn't reach the stream right now.").set_should_end_session(True)
            return handler_input.response_builder.response

        return util.play(
            url=url,
            offset=0,
            text=None,
            response_builder=handler_input.response_builder,
            supports_apl=supports_apl
        )


class NextOrPreviousCommandHandler(AbstractRequestHandler):
    """Handler for Next or Previous command from hardware buttons or touch
    control.

    This handler handles the next/previous command sent through hardware
    buttons such as remote control or the next/previous control from
    Alexa-devices with a screen.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return (is_request_type(
            "PlaybackController.NextCommandIssued")(handler_input) or
                is_request_type(
                    "PlaybackController.PreviousCommandIssued")(handler_input))

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In NextOrPreviousCommandHandler")
        return handler_input.response_builder.response


class PauseCommandHandler(AbstractRequestHandler):
    """Handler for Pause command from hardware buttons or touch control.

    This handler handles the pause command sent through hardware
    buttons such as remote control or the pause control from
    Alexa-devices with a screen.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return is_request_type("PlaybackController.PauseCommandIssued")(
            handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In PauseCommandHandler")
        return util.stop(text=None,
                         response_builder=handler_input.response_builder,
                         supports_apl=supports_apl)

# ###################################################################

# ################## EXCEPTION HANDLERS #############################
class CatchAllExceptionHandler(AbstractExceptionHandler):
    """Catch all exception handler, log exception and
    respond with custom message.
    """
    def can_handle(self, handler_input, exception):
        # type: (HandlerInput, Exception) -> bool
        return True

    def handle(self, handler_input, exception):
        # type: (HandlerInput, Exception) -> Response
        logger.info("In CatchAllExceptionHandler")
        logger.error(exception, exc_info=True)
        _ = handler_input.attributes_manager.request_attributes["_"]
        handler_input.response_builder.speak(_(data.UNHANDLED_MSG)).ask(
            _(data.HELP_MSG).format(
                util.audio_data(handler_input.request_envelope.request)))

        return handler_input.response_builder.response

# ###################################################################

# ############# REQUEST / RESPONSE INTERCEPTORS #####################

class APLSupportRequestInterceptor(AbstractRequestInterceptor):
    """Request Interceptor to check if the device supports APL and update the global supports_apl variable."""
    def process(self, handler_input):
        global supports_apl
        if hasattr(handler_input, 'request_envelope'):
            supported_interfaces = getattr(
                handler_input.request_envelope.context.system.device.supported_interfaces,
                'alexa_presentation_apl', None)
            supports_apl = supported_interfaces is not None
        else:
            supports_apl = False

class DeviceSeenInterceptor(AbstractRequestInterceptor):
    """Record every device that talks to the skill, for the device-assignment setup page.

    Any request type registers the device - the user doesn't need to say
    "weiterhören" specifically just to get their Echo listed for assignment.
    """
    def process(self, handler_input):
        try:
            device_id = handler_input.request_envelope.context.system.device.device_id
            device_registry.record_seen(device_id)
        except Exception:
            logger.exception("Failed to record seen device")


class RequestLogger(AbstractRequestInterceptor):
    """Log the alexa requests."""
    def process(self, handler_input):
        # type: (HandlerInput) -> None
        request = handler_input.request_envelope.request
        try:
            req_type = getattr(request, 'object_type', type(request).__name__)
            # Skip noisy APL UserEvent logs.
            if req_type == "Alexa.Presentation.APL.UserEvent":
                return

            # If this is an IntentRequest, log intent name and slots
            if hasattr(request, 'intent') and request.intent:
                intent_name = getattr(request.intent, 'name', None)
                slots = {}
                intent_slots = getattr(request.intent, 'slots', None)
                if intent_slots:
                    for slot_key, slot_obj in intent_slots.items():
                        slots[slot_key] = getattr(slot_obj, 'value', None)

                logger.info("Incoming Intent: %s - Slots: %s", intent_name, slots)
            else:
                logger.info("Incoming Request Type: %s", req_type)
        except Exception:
            logger.exception("Failed to log incoming request details")

        # Keep a debug-level dump of the full request for deep troubleshooting
        logger.debug("Alexa Request: %s", request)


class LocalizationInterceptor(AbstractRequestInterceptor):
    """Process the locale in request and load localized strings for response.

    This interceptors processes the locale in request, and loads the locale
    specific localization strings for the function `_`, that is used during
    responses.
    """
    def process(self, handler_input):
        # type: (HandlerInput) -> None
        locale = getattr(handler_input.request_envelope.request, 'locale', None)
        if locale:
            parts = locale.split("-")
            lang = parts[0]
            region = parts[1] if len(parts) > 1 else None
        
            mapping = {
                "fr": "fr-CA" if region == "CA" else "fr-FR",
                "it": "it-IT",
                "es": "es-ES",
                "pt": "pt-BR",
                "de": "de-DE",
            }
        
            locale_file_name = mapping.get(lang, locale)

            i18n = gettext.translation(
                'data', localedir='locales', languages=[locale_file_name],
                fallback=True)
            handler_input.attributes_manager.request_attributes[
                "_"] = i18n.gettext
        else:
            handler_input.attributes_manager.request_attributes[
                "_"] = gettext.gettext


class ResponseLogger(AbstractResponseInterceptor):
    """Log the alexa responses."""
    def process(self, handler_input, response):
        # type: (HandlerInput, Response) -> None
        logger.debug("Alexa Response: {}".format(response))

# ###################################################################


# ############# REGISTER HANDLERS #####################
# Request Handlers
sb.add_request_handler(CheckAudioInterfaceHandler())
sb.add_request_handler(SkillEventHandler())
sb.add_request_handler(LaunchRequestOrPlayAudioHandler())
sb.add_request_handler(ContinueAudiobookIntentHandler())
sb.add_request_handler(PlaySearchIntentHandler())
sb.add_request_handler(PlayCommandHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(ExceptionEncounteredHandler())
sb.add_request_handler(APLUserEventHandler())
sb.add_request_handler(UnhandledIntentHandler())
sb.add_request_handler(NextOrPreviousIntentHandler())
sb.add_request_handler(NextOrPreviousCommandHandler())
sb.add_request_handler(PauseIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(PauseCommandHandler())
sb.add_request_handler(ResumeIntentHandler())
sb.add_request_handler(StartOverIntentHandler())
sb.add_request_handler(PlaybackStartedHandler())
sb.add_request_handler(PlaybackFinishedHandler())
sb.add_request_handler(PlaybackStoppedHandler())
sb.add_request_handler(PlaybackNearlyFinishedHandler())
sb.add_request_handler(PlaybackFailedHandler())

# Exception handlers
sb.add_exception_handler(CatchAllExceptionHandler())

# Interceptors
sb.add_global_request_interceptor(APLSupportRequestInterceptor())
sb.add_global_request_interceptor(DeviceSeenInterceptor())
sb.add_global_request_interceptor(RequestLogger())
sb.add_global_request_interceptor(LocalizationInterceptor())
sb.add_global_response_interceptor(ResponseLogger())

# AWS Lambda handler
lambda_handler = sb.lambda_handler()
