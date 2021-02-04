#!/usr/bin/env python3
"""
This script downloads the latest MTG card data from http://mtgjson.com/ and processes
it to turn the highly-structured data there into a flat list of card names to descriptions
formatted to send down the chat.
"""

import common
common.FRAMEWORK_ONLY = True
import urllib.request
import urllib.error
import urllib.parse
import json
import psycopg2
from collections import defaultdict

from common.config import config
from common.card import clean_text, CARD_GAME_PTCG

TYPEABBR = {
	"Colorless": "C",
	"Darkness": "D",
	"Dragon": "N",
	"Grass": "G",
	"Fairy": "Y",
	"Fighting": "F",
	"Fire": "R",
	"Lightning": "L",
	"Metal": "M",
	"Psychic": "P",
	"Water": "W",
}

URL = "https://api.pokemontcg.io/v2/cards?pageSize=250&page=%d"

def main():
	with psycopg2.connect(config['postgres']) as conn, conn.cursor() as cur:
		cur.execute("DELETE FROM cards WHERE game = %s", (CARD_GAME_PTCG, ))

		for cardname, filteredname, description, hidden, card in iter_cards():
			cardname, filteredname, skip = get_hacks(cardname, filteredname, card)
			if skip:
				continue

			cur.execute("INSERT INTO cards (game, filteredname, name, text, hidden) VALUES (%s, %s, %s, %s, %s)", (
				CARD_GAME_PTCG,
				filteredname,
				cardname,
				description,
				hidden,
			))

def iter_cards():
	for group in group_cards():
		for card in group:
			yield from process_card(card, group)

def fetch_cards():
	page = 1
	count = 0
	total = 0
	while True:
		print(f"{count}/{total}")
		req = urllib.request.Request(URL % page, headers={
			'User-Agent': "LRRbot/2.0 (https://lrrbot.com/)",
		})
		with urllib.request.urlopen(req) as fp:
			dat = json.load(fp)
		if not dat['data']:
			break
		yield from dat['data']
		count += len(dat['data'])
		total = dat['totalCount']
		page += 1

def group_cards():
	pkmn_cards = []
	other_cards = []
	for card in fetch_cards():
		if card['supertype'] == 'Pokémon':
			pkmn_cards.append(card)
		else:
			other_cards.append(card)
	yield from group_pkmn_cards(pkmn_cards)
	yield from group_other_cards(other_cards)

def group_pkmn_cards(cards):
	"""
	There can be multiple Pokemon cards with the same name... group them by name
	so that we know if a given card name is a unique or not.
	"""
	names = defaultdict(list)
	for card in cards:
		names[clean_text(card['name'])].append(card)
	return list(names.values())

def group_other_cards(cards):
	"""
	There can not be multiple non-Pokemon cards with the same name... if there are
	multiple cards with the same name, pick up only the most recent printing, as
	it is the authoritative one.
	"""
	names = defaultdict(list)
	for card in cards:
		names[clean_text(card['name'])].append(card)
	return [
		[max(group, key=lambda card:card['set']['releaseDate'])]
		for group in names.values()
	]

def process_card(card, group):
	unique = len(group) <= 1
	setunique = len([c for c in group if c['set']['id'] == card['set']['id']]) <= 1
	description = ''.join(gen_text(card))
	for cardname, hidden in gen_cardnames(card, unique, setunique):
		filteredname = clean_text(cardname)
		yield cardname, filteredname, description, hidden, card

def gen_cardnames(card, unique, setunique):
	"""
	For non-Pokemon cards, just generate the card name, since that's enough to
	identify the card.
	For Pokemon:
	  * If it's the only Pokemon with that name, generate "Name" as visible
	    but also "Name (set)" and "Name (set num)" as hidden.
	  * If it's the only Pokemon with that name in the set, generate "Name (set)"
	    but also "Name (set num)" as hidden.
	  * Otherwise, only generate "Name (set num)" as visible.
	"""
	if card['supertype'] == 'Pokémon':
		yield f"{card['name']} ({setcode(card)} {card['number']})", unique or setunique
		if setunique:
			yield f"{card['name']} ({setcode(card)})", unique
		if unique:
			yield f"{card['name']}", False
	else:
		yield f"{card['name']}", False

def gen_text(card):
	yield card['name']
	if card['supertype'] == 'Pokémon':
		yield ' ('
		yield setcode(card)
		yield ' '
		yield str(card['number'])
		yield ')'
	yield ' | '
	yield card['supertype']
	if card.get('types'):
		yield ' {'
		yield '/'.join(card['types'])
		yield '}'
	if card.get('hp'):
		yield ' ['
		yield card['hp']
		yield 'HP]'
	if card.get('subtypes'):
		yield ' \u2014 '
		yield ' '.join(card['subtypes'])
	if card.get('evolvesFrom'):
		yield ' [evolves from: '
		yield card['evolvesFrom']
		yield ']'
	if card.get('rules'):
		yield ' | '
		for i, rule in enumerate(card['rules']):
			if i:
				yield ' / '
			yield rule
	rules = []
	if card.get('ancientTrait'):
		card['ancientTrait']['type'] = 'Ancient Trait'
		rules.append(card['ancientTrait'])
	if card.get('abilities'):
		rules.extend(card['abilities'])
	if card.get('attacks'):
		rules.extend(card['attacks'])
	if rules:
		yield ' | '
		for i, rule in enumerate(rules):
			if i:
				yield ' / '
			if rule.get('type') and rule['type'] != 'Ability':
				yield '['
				yield rule['type']
				yield '] '
			if 'cost' in rule:
				yield '{'
				yield cost(rule['cost'])
				yield '} '
			yield rule['name']
			if rule.get('damage'):
				yield ' ('
				yield rule['damage']
				yield ')'
			if rule.get('text'):
				yield ': '
				yield rule['text']
		if card.get('weaknesses'):
			yield ' | weak: '
			for i, typeeff in enumerate(card.get('weaknesses')):
				if i:
					yield ', '
				yield typeeff['type']
				yield typeeff['value']
		if card.get('resistances'):
			yield ' | resist: '
			for i, typeeff in enumerate(card.get('resistances')):
				if i:
					yield ', '
				yield typeeff['type']
				yield typeeff['value']
		if 'retreatCost' in card:
			yield ' | retreat: {'
			yield cost(card['retreatCost'])
			yield '}'

def setcode(card):
	return card['set'].get('ptcgoCode') or card['set']['id'].upper()

def cost(costs, colorlesscount=True):
	res = []
	if colorlesscount:
		costs = [i for i in costs if i != 'Free']
		count = len([i for i in costs if i == 'Colorless'])
		if count or not costs:
			res.append(str(count))
			costs = [i for i in costs if i != 'Colorless']
	for cost in costs:
		res.append(TYPEABBR[cost])
	return ''.join(res)

def get_hacks(cardname, filteredname, card):
	skip = False

	# Diddle with the Unowns so that these don't have the same filtered name
	if card['name'] == 'Unown' and card['number'] == '!':
		filteredname += 'exclamation'
	if card['name'] == 'Unown' and card['number'] == '?':
		filteredname += 'question'

	# Don't generate the un-disambiged names for these "ex" pokemon
	# that have the same name as pokemon from the "EX" expansion
	if card['name'] in ('Chansey ex', 'Electabuzz ex', 'Magmar ex') and cardname == card['name']:
		skip = True

	return cardname, filteredname, skip

if __name__ == '__main__':
	main()
