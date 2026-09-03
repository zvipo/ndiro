"""Outbound email via Amazon SES (native-account verification/reset mail).

Uses the same boto3 credential chain as db.py's DynamoDB/S3 handles; the only
extra configuration is MAIL_FROM (an SES-verified identity — its absence
disables every mail-sending flow, see config.EMAIL_ENABLED) and optionally
SES_REGION / APP_BASE_URL.

Logging discipline (invariant #8): a failure logs `MAIL_ERROR` with the
exception type and the provider's error code — facts about the API call —
never the recipient address, subject, body, or any emailed token (the link
in a verification/reset mail is a live capability).
"""
import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from flask import request

import config

# Lazy like db.py's handles: a missing/broken AWS config degrades at request
# time instead of crashing boot. Tests monkeypatch `send` and never build one.
_ses = None


def _ses_client():
    global _ses
    if _ses is None:
        _ses = boto3.client(
            'sesv2',
            region_name=config.SES_REGION,
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
            # Tight timeouts: a hung SES call must not eat gunicorn's 60s.
            config=BotoConfig(connect_timeout=5, read_timeout=10,
                              retries={'max_attempts': 1}),
        )
    return _ses


def enabled():
    return config.EMAIL_ENABLED


def base_url():
    """Absolute base for links in emails. APP_BASE_URL when set (immune to
    Host-header forgery). The request-host fallback is reachable ONLY in the
    COOKIE_SECURE=0 dev mode: config.EMAIL_ENABLED requires APP_BASE_URL in
    production, so a forged Host can never steer a production reset link."""
    if config.APP_BASE_URL:
        return config.APP_BASE_URL
    return request.host_url.rstrip('/')


def send(to_addr, subject, body):
    """Send one plaintext email. Returns True on success, False on any
    failure (callers show a generic message and offer a resend)."""
    if not enabled():
        return False
    try:
        _ses_client().send_email(
            FromEmailAddress=config.MAIL_FROM,
            Destination={'ToAddresses': [to_addr]},
            Content={'Simple': {
                'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                'Body': {'Text': {'Data': body, 'Charset': 'UTF-8'}},
            }},
        )
        return True
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', 'unknown')
        print(f"MAIL_ERROR ClientError {code}")
        return False
    except Exception as e:
        print(f"MAIL_ERROR {type(e).__name__}")
        return False
