import os
import asyncio
from cairosvg import svg2png
import chess
import chess.engine
import chess.svg
from collections import Counter
import configparser
import discord
import io
import random
import requests
import shelve

client = discord.Client()

boards = {}
delays = {}
difficulties = {}
votes = {}

config = configparser.ConfigParser()
config.read('config.ini')

@client.event
async def on_ready():
    await client.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name='chess help'))
    print(f'{client.user} has connected to Discord')

    global transport
    global engine
    transport = {}
    engine = {}
    transport['easy'], engine['easy'] = await chess.engine.popen_uci(config['easy']['engine'])
    transport['normal'], engine['normal'] = await chess.engine.popen_uci(config['normal']['engine'])
    transport['hard'], engine['hard'] = await chess.engine.popen_uci(config['hard']['engine'])
    print('Engines started')

    global d
    d = shelve.open('data')
    if 'boards' in d:
        global boards
        boards = d['boards']
        for channel_id in boards:
            channel = client.get_channel(channel_id)
            if(channel is not None):
                asyncio.ensure_future(channel.send('Due to a system restart all votes have been lost. Please vote again.'))
    if 'delays' in d:
        global delays
        delays = d['delays']
    if 'difficulties' in d:
        global difficulties
        difficulties = d['difficulties']
    print('Data loaded')

@client.event
async def on_message(message):
    if message.author.bot:
        return

    channel = message.channel

    split_content = message.content.lower().split(' ')

    if split_content[0] == 'chess':
        if split_content[1] == 'setvotingtime':
            if not isinstance(channel, discord.channel.DMChannel):
                if channel.permissions_for(message.author).administrator or channel.permissions_for(message.author).manage_guild:
                    if len(split_content) > 2:
                        delay = int(split_content[2])
                        delays[channel.guild.id] = delay
                        await channel.send(f'Set voting time after first vote to {delay/60} minutes.')
                        asyncio.ensure_future(save_data())
                    else:
                        await channel.send('Please specify voting time in seconds: **chess setvotingtime <seconds>**')
                else:
                    await channel.send('You must have the permission **Administrator** or **Manage Server** to use this command')
            else:
                await channel.send('This command is disabled for DMs')

        elif split_content[1] == 'start':
            if channel.id in boards:
                await channel.send('A game is still ongoing... please end to start a new game.')
            else:
                await channel.send('Starting a new game...')
                board = boards[channel.id] = chess.Board()

                if 'white' in split_content:
                    color = 0
                elif 'black' in split_content:
                    color = 1
                else:
                    color = random.randint(0, 1)

                if 'easy' in split_content:
                    difficulty = 'easy'
                elif 'hard' in split_content:
                    difficulty = 'hard'
                else:
                    difficulty = 'normal'

                difficulties[channel.id] = difficulty

                if color:
                    result = await engine[difficulty].play(boards[channel.id], chess.engine.Limit(time=parse_float(config[difficulty].get('time'), 1), depth=parse_int(config[difficulty].get('depth')), nodes=parse_int(config[difficulty].get('nodes'))))
                    board.push(result.move)

                await send_board(channel)

                asyncio.ensure_future(save_data())

        elif split_content[1] == 'board':
            if channel.id in boards:
                await send_board(channel)
            else:
                await channel.send('No ongoing game. Use **chess start** to start a game.')

        elif split_content[1] == 'help':
            embed = discord.Embed(title='Coop Chess Help', description='All commands for the Coop Chess Bot')
            embed.add_field(name='Commands', value='Start game: **chess start <easy|normal|hard> <white|black|random>**\nShow chess board: **chess board**\nSet voting time after first vote: **chess setvotingtime <seconds>**\nShow this help page: **chess help**', inline=False)
            embed.add_field(name='Voting', value='During a game the following votes are possible:\nUse [algebraic notation](https://en.wikipedia.org/wiki/Algebraic_notation_(chess)#Notation_for_moves) to make a move (e.g. **Rhd7**)\nVote to resign: **resign**\nVote to claim draw: **draw**', inline=False)
            embed.add_field(name='Engines', value=f'Easy: {config["easy"]["description"]}\nNormal: {config["normal"]["description"]}\nHard: {config["hard"]["description"]}')
            embed.add_field(name='Support Coop Chess', value='[Invite bot](https://discord.com/api/oauth2/authorize?client_id=812815811801776129&permissions=59392&scope=bot)\n[GitHub](https://github.com/caheuer/discord-coop-chess)', inline=False)
            await channel.send(embed=embed)

        else:
            await channel.send('Command not found. Use **chess help** to list commands.')

        return
    
    if message.content.lower() == 'resign':
        move = 'resign'
    elif message.content.lower() == 'draw' and boards[channel.id].can_claim_draw():
        move = 'draw'
    else:
        try:
            move = boards[channel.id].parse_san(message.content)
        except:
            return

    if isinstance(move, str) or move in boards[channel.id].legal_moves:
        if channel.id not in votes:
            votes[channel.id] = {}

            if isinstance(channel, discord.channel.DMChannel):
                delay = 0
            else:
                if channel.guild.id not in delays:
                    delays[channel.guild.id] = config['general'].getint('standard_voting_time', 300)
                delay = delays[channel.guild.id]

            asyncio.ensure_future(execute_move(channel, delay))
            if delay > 0:
                await channel.send(f'First legal move vote received, waiting {delay//60} minute(s) for further votes...')

        votes[channel.id][message.author.id] = move

    await message.delete()

async def execute_move(channel, delay):
    # wait
    if delay < 1:
        delay = 1
    await asyncio.sleep(delay)

    board = boards[channel.id]

    vote_count = Counter(votes[channel.id].values())

    move = max(vote_count, key=lambda x: vote_count.get(x) + random.random())

    if not isinstance(channel, discord.channel.DMChannel):
        await channel.send('Votes: ' + ', '.join([f'{key} - {value}' for key, value in vote_count.items()]))

    if move == 'resign':
        await channel.send('Game over - resigned')

        if len(board.move_stack) > 1:
            response = requests.post('https://lichess.org/import', data={'pgn': chess.Board().variation_san(board.move_stack)})
            await channel.send(response.url)

        boards.pop(channel.id)
        votes.pop(channel.id)
        asyncio.ensure_future(save_data())

        return

    if move == 'draw':
        await check_board(channel, claim_draw=True)
        return
    
    board.push(move)

    if await check_board(channel, reverse=True):
        return

    difficulty = difficulties[channel.id]
    result = await engine[difficulty].play(boards[channel.id], chess.engine.Limit(time=parse_float(config[difficulty].get('time'), 1), depth=parse_int(config[difficulty].get('depth')), nodes=parse_int(config[difficulty].get('nodes'))))
    board.push(result.move)

    if await check_board(channel):
        return

    await send_board(channel, result.move)

    # reset votes
    votes.pop(channel.id)

async def check_board(channel, claim_draw=False, reverse=False):
    board = boards[channel.id]

    if board.is_game_over() or claim_draw:
        await send_board(channel, reverse=reverse)

        if(board.result(claim_draw=claim_draw) == '1-0'):
            result = 'white wins'
        elif(board.result(claim_draw=claim_draw) == '0-1'):
            result = 'black wins'
        elif(board.is_stalemate()):
            result = 'stalemate'
        elif(board.is_insufficient_material()):
            result = 'draw (insufficient material)'
        elif(board.is_seventyfive_moves()):
            result = 'draw (75 moves since last capture or pawn move)'
        elif(board.is_fivefold_repetition()):
            result = 'draw (fivefold repetition)'
        elif(board.can_claim_fifty_moves()):
            result = 'draw (50 moves since last capture or pawn move)'
        elif(board.can_claim_threefold_repetition()):
            result = 'draw (threefold repetition)'
        else:
            result = 'draw'

        await channel.send('Game over - ' + result)

        response = requests.post('https://lichess.org/import', data={'pgn': chess.Board().variation_san(board.move_stack)})
        await channel.send(response.url)

        boards.pop(channel.id)
        votes.pop(channel.id)

        asyncio.ensure_future(save_data())

        return True

    asyncio.ensure_future(save_data())
    
    return False

async def send_board(channel, move=None, reverse=False):
    board = boards[channel.id]

    if reverse:
        if board.turn == chess.WHITE:
            turn = chess.BLACK
        else:
            turn = chess.WHITE
    else:
        turn = board.turn

    svg_image = io.StringIO(chess.svg.board(board, orientation=turn, lastmove=move, size=350))

    png_image = io.BytesIO(svg2png(file_obj=svg_image))

    file = discord.File(png_image, 'chess.png')

    await channel.send('', file=file)

async def save_data():
    d['boards'] = boards
    d['delays'] = delays
    d['difficulties'] = difficulties
    d.sync()

def parse_int(val, fallback=None):
    try:
        return int(val)
    except ValueError:
        return fallback

def parse_float(val, fallback=None):
    try:
        return float(val)
    except ValueError:
        return fallback

client = client.run(config['general']['discord_token'])