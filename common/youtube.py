import asyncio
import datetime
import json

import pytz
import sqlalchemy

from common import http
from common import postgres
from common.account_providers import ACCOUNT_PROVIDER_YOUTUBE
from common.config import config


async def get_token(channel_id):
	engine, metadata = postgres.get_engine_and_metadata()
	accounts = metadata.tables["accounts"]
	with engine.connect() as conn:
		account_id, access_token, refresh_token, token_expires_at = conn.execute(
			sqlalchemy.select(accounts.c.id, accounts.c.access_token, accounts.c.refresh_token, accounts.c.token_expires_at)
			.where(accounts.c.provider == ACCOUNT_PROVIDER_YOUTUBE)
			.where(accounts.c.provider_user_id == channel_id)
		).one()

		if token_expires_at > datetime.datetime.now(pytz.utc):
			return access_token

		access_token, refresh_token, token_expires_at = await request_token('refresh_token', refresh_token=refresh_token)

		update = {
			'access_token': access_token,
			'token_expires_at': token_expires_at,
		}
		if refresh_token:
			update['refresh_token'] = refresh_token

		conn.execute(accounts.update().where(accounts.c.id == account_id), update)
		conn.commit()

		return access_token

async def request_token(grant_type, **data):
	data['grant_type'] = grant_type
	data['client_id'] = config['youtube_client_id']
	data['client_secret'] = config['youtube_client_secret']

	data = await http.request('https://oauth2.googleapis.com/token', method='POST', data=data)
	data = json.loads(data)

	print(data)

	access_token = data['access_token']
	refresh_token = data.get('refresh_token')
	expiry = datetime.datetime.now(pytz.utc) + datetime.timedelta(seconds=data['expires_in'])

	return access_token, refresh_token, expiry

async def get_my_channel(access_token, parts=['snippet']):
	"""
	Get the authorized user's channel.

	Docs: https://developers.google.com/youtube/v3/docs/channels/list
	"""
	data = await http.request(
		'https://youtube.googleapis.com/youtube/v3/channels',
		data={'part': ','.join(parts), 'mine': 'true'},
		headers={'Authorization': f'Bearer {access_token}'})
	return json.loads(data)['items'][0]

async def get_paginated(url, data, channel_id):
	while True:
		headers = {'Authorization': f'Bearer {await get_token(channel_id)}'}
		response = json.loads(await http.request(url, data=data, headers=headers))
		for item in response['items']:
			yield item
		if next_page_token := response.get('nextPageToken'):
			data['pageToken'] = next_page_token
			# Only present on `LiveChatMessages: list`
			if (delay := response.get('pollingIntervalMillis', 0) / 1000) > 0:
				await asyncio.sleep(delay)
		else:
			break

async def get_user_broadcasts(channel_id, parts=['snippet']):
	"""
	Get the user's broadcasts.

	Docs: https://developers.google.com/youtube/v3/live/docs/liveBroadcasts/list
	"""
	broadcasts = get_paginated(
		'https://youtube.googleapis.com/youtube/v3/liveBroadcasts',
		{'part': ','.join(parts), 'mine': 'true'},
		channel_id,
	)
	async for broadcast in broadcasts:
		yield broadcast

async def get_chat_page(channel_id, live_chat_id, page_token=None, parts=['snippet', 'authorDetails']):
	"""
	Get live chat messages for a specific chat.

	Docs: https://developers.google.com/youtube/v3/live/docs/liveChatMessages/list
	"""
	data = {'liveChatId': live_chat_id, 'part': ','.join(parts), 'maxResults': '2000'}
	if page_token:
		data['pageToken'] = page_token
	headers = {'Authorization': f'Bearer {await get_token(channel_id)}'}
	return json.loads(await http.request('https://www.googleapis.com/youtube/v3/liveChat/messages', data=data, headers=headers))

def check_message_length(message, max_len = 200):
	# The limit is documented as '200 characters'.
	length = message.encode('utf-16-le') // 2
	return length <= max_len

async def send_chat_message(channel_id, chat_id, message):
	"""
	Send a text message to a live chat.

	Returns the created LiveChatMessage object.

	Docs: https://developers.google.com/youtube/v3/live/docs/liveChatMessages/insert
	"""
	headers = {'Authorization': f'Bearer {await get_token(channel_id)}'}
	return json.loads(await http.request('https://www.googleapis.com/youtube/v3/liveChat/messages?part=snippet', method='POST', asjson=True, headers=headers, data={
		'snippet': {
			'liveChatId': chat_id,
			'type': 'textMessageEvent',
			'textMessageDetails': {
				'messageText': message,
			},
		},
	}))
