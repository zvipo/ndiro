"""AI viscous-fiber estimators: OpenAI chat completions via plain requests
(no SDK), strict json_schema output. Optional feature — an unset
OPENAI_API_KEY hides the buttons and 400s the endpoints.

The per-user daily cap lives in db.try_consume_ai_use / refund_ai_use;
app.py consumes a use BEFORE calling these and refunds on upstream failure.
"""
import base64
import json

import requests

import config


def _fiber_guide_prompt():
    """The guide table as prompt text, one food per line."""
    return '\n'.join(
        f"{'* ' if f['star'] else ''}{f['name']} — {f['serving']} — {f['grams']} g"
        for f in config.FIBER_GUIDE
    )


def _estimator_system_prompt():
    return (
        'You estimate VISCOUS SOLUBLE fiber (not total dietary fiber) in a logged '
        'meal, using this dietician guide (grams of viscous soluble fiber per '
        'serving; * marks foods with 3 g or more per serving):\n\n'
        + _fiber_guide_prompt() +
        '\n\nDaily context: minimum target 5-10 g/day; Portfolio Diet goal '
        f'{config.VISCOUS_FIBER_GOAL_G} g/day.\n'
        'Rules:\n'
        '- Prefer guide values; scale linearly by portion (half a serving = half the grams).\n'
        '- Foods not in the guide get a conservative (low) estimate; meat, dairy, '
        'eggs, oils and refined grains are 0.\n'
        '- viscous_fiber_g must equal the sum of the item grams, rounded to 1 decimal.\n'
        '- note: one short sentence on assumptions/uncertainty, or "" if none.'
    )


def _estimate_schema(with_description=False):
    """Strict json_schema for the estimator response (optionally + description)."""
    properties = {
        'viscous_fiber_g': {'type': 'number'},
        'items': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['food', 'serving', 'grams'],
                'properties': {
                    'food': {'type': 'string'},
                    'serving': {'type': 'string'},
                    'grams': {'type': 'number'},
                },
            },
        },
        'note': {'type': 'string'},
    }
    if with_description:
        properties['description'] = {'type': 'string'}
    return {
        'type': 'json_schema',
        'json_schema': {
            'name': 'fiber_estimate',
            'strict': True,
            'schema': {
                'type': 'object',
                'additionalProperties': False,
                'required': sorted(properties),
                'properties': properties,
            },
        },
    }


def _openai_estimate(messages, with_description=False, timeout=(5, 20)):
    """Shared OpenAI chat-completions call for the estimators.

    Returns (result_dict, None) or (None, (message, http_status, refundable)).
    `refundable` is True when the failure was upstream (timeout / non-200) —
    the caller then refunds the consumed AI use. A 200 we cannot parse is NOT
    refundable (the upstream call was made and billed).

    No temperature/max_tokens: the gpt-5 family rejects non-default
    temperature, and the strict schema keeps output short anyway.
    """
    payload = {
        'model': config.OPENAI_MODEL,
        'messages': messages,
        'response_format': _estimate_schema(with_description),
    }
    try:
        # Read timeout stays well under gunicorn's 60s worker timeout.
        resp = requests.post(
            config.OPENAI_CHAT_URL,
            headers={'Authorization': f'Bearer {config.OPENAI_API_KEY}'},
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as e:
        print(f"OpenAI request failed: {type(e).__name__}")
        return None, ('AI estimate failed — could not reach the estimator', 502, True)
    if resp.status_code != 200:
        print(f"OpenAI error {resp.status_code}")
        return None, ('AI estimate failed (upstream error)', 502, True)

    try:
        content = resp.json()['choices'][0]['message']['content']
        raw = json.loads(content)
        result = {
            'viscous_fiber_g': max(round(float(raw['viscous_fiber_g']), 1), 0.0),
            'items': [
                {'food': str(i['food']), 'serving': str(i['serving']),
                 'grams': round(float(i['grams']), 1)}
                for i in raw.get('items', [])
            ],
            'note': str(raw.get('note', '')),
            'model': config.OPENAI_MODEL,
        }
        if with_description:
            result['description'] = str(raw.get('description', '')).strip()[:500]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        print(f"OpenAI response parse failed: {type(e).__name__}")
        return None, ('AI estimate returned an unreadable response', 502, False)
    return result, None


def estimate_text(description):
    """Estimate viscous fiber from a meal description."""
    return _openai_estimate([
        {'role': 'system', 'content': _estimator_system_prompt()},
        {'role': 'user', 'content': description},
    ])


def estimate_photo(photo_bytes):
    """Describe a meal photo and estimate its viscous fiber (vision).

    photo_bytes must already be normalized JPEG (the route calls imaging.to_jpeg)."""
    data_url = 'data:image/jpeg;base64,' + base64.b64encode(photo_bytes).decode('ascii')
    system = (
        _estimator_system_prompt() +
        '\n- description: a short plain-text meal description from the photo — the '
        'foods and rough portions visible (max 200 characters, no markdown). Name '
        'only the foods and portions: do not mention plates, bowls, cups, cutlery, '
        'packaging, the table, or the setting. Base the fiber items only on what '
        'is visible.\n'
        '- If a coin (e.g. a US quarter, 24mm), payment card (86x54mm), or standard '
        'cutlery is visible, treat it as a scale reference to calibrate portion '
        'sizes — do not list it as food or mention it in the description.'
    )
    return _openai_estimate(
        [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': 'Describe this meal and estimate its viscous soluble fiber.'},
                {'type': 'image_url', 'image_url': {'url': data_url}},
            ]},
        ],
        with_description=True,
        timeout=(5, 25),  # vision runs a bit longer; gunicorn allows 60s
    )
