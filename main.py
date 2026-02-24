#!/usr/bin/env python3
# Developer: 42saph

import asyncio
import sys
import os
import json
import time
import aiohttp
from datetime import datetime
from collections import deque

import discord
from discord.ext import commands
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align
from rich import box

try:
    from config import USER_TOKEN, BOT_TOKEN
    from banner import BANNER
    CONFIG_AVAILABLE = True
except ImportError:
    USER_TOKEN = ""
    BOT_TOKEN = ""
    BANNER = "[red]missing config files[/red]"
    CONFIG_AVAILABLE = False


console = Console()


class RateLimiter:
    def __init__(self):
        self.last = 0
        self.delay = 0.6
    
    async def wait(self):
        now = time.time()
        elapsed = now - self.last
        if elapsed < self.delay:
            await asyncio.sleep(self.delay - elapsed)
        self.last = time.time()


class APIScraper:
    def __init__(self, token):
        self.token = token
        self.session = None
        self.base = "https://discord.com/api/v10"
        self.headers = {
            "Authorization": token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
    
    async def init(self):
        self.session = aiohttp.ClientSession(headers=self.headers)
    
    async def close(self):
        if self.session:
            await self.session.close()
    
    async def get(self, endpoint):
        try:
            async with self.session.get(f"{self.base}{endpoint}") as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 429:
                    retry = float(resp.headers.get("Retry-After", 5))
                    await asyncio.sleep(retry)
                    return await self.get(endpoint)
                elif resp.status == 403:
                    return {"error": "forbidden", "status": 403}
                elif resp.status == 401:
                    return {"error": "unauthorized", "status": 401}
                else:
                    return {"error": f"http {resp.status}", "status": resp.status}
        except Exception as e:
            return {"error": str(e)}
    
    async def scrape_server(self, guild_id):
        guild = await self.get(f"/guilds/{guild_id}?with_counts=true")
        channels = await self.get(f"/guilds/{guild_id}/channels")
        roles = await self.get(f"/guilds/{guild_id}/roles")
        emojis = await self.get(f"/guilds/{guild_id}/emojis")
        
        if guild is None:
            guild = {"error": "no response"}
        if channels is None:
            channels = []
        if roles is None:
            roles = []
        if emojis is None:
            emojis = []
        
        return {
            "id": guild_id,
            "timestamp": datetime.now().isoformat(),
            "guild": guild,
            "channels": channels if isinstance(channels, list) else [],
            "roles": roles if isinstance(roles, list) else [],
            "emojis": emojis if isinstance(emojis, list) else []
        }


class Clone:
    def __init__(self, bot):
        self.bot = bot
        self.rate = RateLimiter()
        self.current_operation = "idle"
        self.completed = 0
        self.total = 0
        self.errors = 0
        self.print_lock = asyncio.Lock()
    
    async def log_add(self, msg):
        async with self.print_lock:
            console.print(f"[green][+][/green] {msg}")
    
    async def log_delete(self, msg):
        async with self.print_lock:
            console.print(f"[red][-][/red] {msg}")
    
    async def log_error(self, msg):
        async with self.print_lock:
            console.print(f"[red][x][/red] {msg}")
        self.errors += 1
    
    def print_status(self):
        if self.total > 0:
            percent = (self.completed / self.total) * 100
            status = f"[blue]{self.current_operation}[/blue]: {self.completed}/{self.total} ({percent:.1f}%)"
        else:
            status = f"[blue]{self.current_operation}[/blue]: {self.completed} processed"
        
        if self.errors > 0:
            status += f" | [red]{self.errors} errors[/red]"
        
        console.print(status)
    
    def reset_stats(self):
        self.current_operation = "idle"
        self.completed = 0
        self.total = 0
        self.errors = 0
    
    def safe_int(self, value, default=0):
        if value is None:
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except:
                return default
        if isinstance(value, float):
            return int(value)
        return default
    
    def parse_permission_overwrites(self, overwrites_data, guild_to):
        overwrites_to = {}
        
        for ow in overwrites_data:
            ow_type = ow.get("type", 0)
            source_id = self.safe_int(ow.get("id"), 0)
            allow_val = self.safe_int(ow.get("allow"), 0)
            deny_val = self.safe_int(ow.get("deny"), 0)
            
            if ow_type == 0:
                target_role = self.bot.role_map.get(source_id)
                
                if not target_role and source_id == self.bot.source_id:
                    target_role = guild_to.default_role
                    
                if target_role:
                    allow = discord.Permissions(allow_val)
                    deny = discord.Permissions(deny_val)
                    overwrites_to[target_role] = discord.PermissionOverwrite.from_pair(allow, deny)
            
        return overwrites_to
    
    async def roles_delete(self, guild_to):
        self.current_operation = "Deleting Roles"
        roles = [r for r in guild_to.roles if r.name != "@everyone"]
        self.total = len(roles)
        self.completed = 0
        
        console.print(f"[dim]Deleting {self.total} roles...[/dim]")
        
        for role in roles:
            await self.rate.wait()
            try:
                await role.delete()
                await self.log_delete(f"deleted role: {role.name}")
            except discord.Forbidden:
                await self.log_error(f"forbidden: {role.name}")
            except discord.HTTPException as e:
                await self.log_error(f"http error: {role.name}")
            self.completed += 1
        
        self.print_status()
    
    async def roles_create(self, guild_to, roles_data):
        self.current_operation = "Creating Roles"
        self.total = len(roles_data)
        self.completed = 0
        
        roles_data = sorted(roles_data, key=lambda r: self.safe_int(r.get("position", 0)))
        
        console.print(f"[dim]Creating {self.total} roles...[/dim]")
        
        for role_data in roles_data:
            await self.rate.wait()
            
            name = role_data.get("name", "unnamed")
            if name == "@everyone":
                try:
                    perms_val = self.safe_int(role_data.get("permissions"), 0)
                    perms = discord.Permissions(perms_val)
                    
                    everyone_role = guild_to.default_role
                    await everyone_role.edit(permissions=perms)
                    
                    self.bot.role_map[role_data.get("id")] = everyone_role
                    await self.log_add("updated @everyone permissions")
                except Exception as e:
                    await self.log_error(f"everyone error: {str(e)[:40]}")
                
                self.completed += 1
                continue
            
            try:
                perms_val = self.safe_int(role_data.get("permissions"), 0)
                perms = discord.Permissions(perms_val)
                
                color_val = self.safe_int(role_data.get("color"), 0)
                color = discord.Color(color_val) if color_val else discord.Color.default()
                
                hoist = role_data.get("hoist", False)
                if isinstance(hoist, str):
                    hoist = hoist.lower() == "true"
                
                mentionable = role_data.get("mentionable", False)
                if isinstance(mentionable, str):
                    mentionable = mentionable.lower() == "true"
                
                new_role = await guild_to.create_role(
                    name=name,
                    permissions=perms,
                    colour=color,
                    hoist=hoist,
                    mentionable=mentionable
                )
                
                self.bot.role_map[role_data.get("id")] = new_role
                await self.log_add(f"created role: {name}")
                
            except discord.Forbidden:
                await self.log_error(f"forbidden: {name}")
            except discord.HTTPException:
                await self.log_error(f"http error: {name}")
            except Exception as e:
                await self.log_error(f"error: {str(e)[:40]}")
            
            self.completed += 1
        
        self.print_status()
        
        await self.reorder_roles(guild_to, roles_data)
    
    async def reorder_roles(self, guild_to, roles_data):
        self.current_operation = "Reordering Roles"
        
        for role_data in roles_data:
            source_id = role_data.get("id")
            target_role = self.bot.role_map.get(source_id)
            
            if not target_role or target_role.name == "@everyone":
                continue
            
            try:
                position = self.safe_int(role_data.get("position"), 0)
                await target_role.edit(position=position)
            except Exception:
                pass
    
    async def channels_delete(self, guild_to):
        self.current_operation = "Deleting Channels"
        channels = list(guild_to.channels)
        self.total = len(channels)
        self.completed = 0
        
        console.print(f"[dim]Deleting {self.total} channels...[/dim]")
        
        for channel in channels:
            await self.rate.wait()
            try:
                await channel.delete()
                await self.log_delete(f"deleted channel: {channel.name}")
            except discord.Forbidden:
                await self.log_error(f"forbidden: {channel.name}")
            except discord.HTTPException:
                await self.log_error(f"http error: {channel.name}")
            self.completed += 1
        
        self.print_status()
    
    async def categories_create(self, guild_to, channels_data):
        self.current_operation = "Creating Categories"
        cats_data = [c for c in channels_data if c.get("type") == 4]
        cats_data = sorted(cats_data, key=lambda c: self.safe_int(c.get("position"), 0))
        self.total = len(cats_data)
        self.completed = 0
        
        if self.total == 0:
            return
            
        console.print(f"[dim]Creating {self.total} categories with permissions...[/dim]")
        
        for cat_data in cats_data:
            await self.rate.wait()
            
            name = cat_data.get("name", "unnamed")
            
            try:
                overwrites_to = self.parse_permission_overwrites(
                    cat_data.get("permission_overwrites", []), 
                    guild_to
                )
                
                new_cat = await guild_to.create_category(name=name, overwrites=overwrites_to)
                pos = self.safe_int(cat_data.get("position"), 0)
                await new_cat.edit(position=pos)
                
                self.bot.cat_map[cat_data.get("id")] = new_cat
                await self.log_add(f"created category: {name}")
                
            except discord.Forbidden:
                await self.log_error(f"forbidden: {name}")
            except discord.HTTPException as e:
                await self.log_error(f"http error: {name} - {str(e)[:50]}")
            
            self.completed += 1
        
        self.print_status()
    
    async def channels_create(self, guild_to, channels_data):
        self.current_operation = "Creating Channels"
        chans_data = [c for c in channels_data if c.get("type") != 4]
        chans_data = sorted(chans_data, key=lambda c: self.safe_int(c.get("position"), 0))
        self.total = len(chans_data)
        self.completed = 0
        
        if self.total == 0:
            return
            
        console.print(f"[dim]Creating {self.total} channels with permissions...[/dim]")
        
        for ch_data in chans_data:
            await self.rate.wait()
            
            name = ch_data.get("name", "unnamed")
            chan_type = self.safe_int(ch_data.get("type"), 0)
            parent_id = ch_data.get("parent_id")
            
            category = self.bot.cat_map.get(parent_id) if parent_id else None
            
            overwrites_to = self.parse_permission_overwrites(
                ch_data.get("permission_overwrites", []), 
                guild_to
            )
            
            try:
                pos = self.safe_int(ch_data.get("position"), 0)
                
                if chan_type == 0:
                    new_chan = await guild_to.create_text_channel(
                        name=name,
                        category=category,
                        overwrites=overwrites_to,
                        position=pos,
                        topic=ch_data.get("topic"),
                        slowmode_delay=self.safe_int(ch_data.get("rate_limit_per_user"), 0),
                        nsfw=ch_data.get("nsfw", False) if isinstance(ch_data.get("nsfw"), bool) else False
                    )
                    self.bot.chan_map[ch_data.get("id")] = new_chan
                    await self.log_add(f"created text: {name}")
                    
                elif chan_type == 2:
                    new_chan = await guild_to.create_voice_channel(
                        name=name,
                        category=category,
                        overwrites=overwrites_to,
                        position=pos,
                        bitrate=self.safe_int(ch_data.get("bitrate"), 64000),
                        user_limit=self.safe_int(ch_data.get("user_limit"), 0)
                    )
                    self.bot.chan_map[ch_data.get("id")] = new_chan
                    await self.log_add(f"created voice: {name}")
                    
                elif chan_type == 5:
                    new_chan = await guild_to.create_text_channel(
                        name=name,
                        category=category,
                        overwrites=overwrites_to,
                        position=pos,
                        topic=ch_data.get("topic")
                    )
                    try:
                        await new_chan.edit(type=discord.ChannelType.news)
                    except:
                        pass
                    self.bot.chan_map[ch_data.get("id")] = new_chan
                    await self.log_add(f"created news: {name}")
                    
                elif chan_type == 13:
                    new_chan = await guild_to.create_stage_channel(
                        name=name,
                        category=category,
                        overwrites=overwrites_to,
                        position=pos
                    )
                    self.bot.chan_map[ch_data.get("id")] = new_chan
                    await self.log_add(f"created stage: {name}")
                    
                elif chan_type == 15:
                    new_chan = await guild_to.create_forum(
                        name=name,
                        category=category,
                        overwrites=overwrites_to,
                        position=pos,
                        topic=ch_data.get("topic")
                    )
                    self.bot.chan_map[ch_data.get("id")] = new_chan
                    await self.log_add(f"created forum: {name}")
                    
                else:
                    new_chan = await guild_to.create_text_channel(
                        name=name,
                        category=category,
                        overwrites=overwrites_to,
                        position=pos
                    )
                    self.bot.chan_map[ch_data.get("id")] = new_chan
                    await self.log_add(f"created channel: {name}")
                    
            except discord.Forbidden:
                await self.log_error(f"forbidden: {name}")
            except discord.HTTPException as e:
                await self.log_error(f"http error: {name} - {str(e)[:50]}")
            except Exception as e:
                await self.log_error(f"error: {str(e)[:40]}")
            
            self.completed += 1
        
        self.print_status()
    
    async def emojis_delete(self, guild_to):
        self.current_operation = "Deleting Emojis"
        emojis = list(guild_to.emojis)
        self.total = len(emojis)
        self.completed = 0
        
        if self.total == 0:
            return
            
        console.print(f"[dim]Deleting {self.total} emojis...[/dim]")
        
        for emoji in emojis:
            await self.rate.wait()
            try:
                await emoji.delete()
                await self.log_delete(f"deleted emoji: {emoji.name}")
            except discord.Forbidden:
                await self.log_error(f"forbidden: {emoji.name}")
            except discord.HTTPException:
                await self.log_error(f"http error: {emoji.name}")
            self.completed += 1
        
        self.print_status()
    
    async def emojis_create(self, guild_to, emojis_data):
        self.current_operation = "Creating Emojis"
        self.total = len(emojis_data)
        self.completed = 0
        
        if self.total == 0:
            console.print("[dim]No emojis to create[/dim]")
            return
            
        console.print(f"[dim]Creating {self.total} emojis...[/dim]")
        
        for emoji_data in emojis_data:
            await self.rate.wait()
            
            name = emoji_data.get("name", "unnamed")
            animated = emoji_data.get("animated", False)
            if isinstance(animated, str):
                animated = animated.lower() == "true"
            
            emoji_id = emoji_data.get("id")
            
            if not emoji_id:
                await self.log_error(f"no emoji id for: {name}")
                self.completed += 1
                continue
            
            try:
                ext = "gif" if animated else "png"
                url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}"
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            image = await resp.read()
                            await guild_to.create_custom_emoji(name=name, image=image)
                            await self.log_add(f"created emoji: {name}")
                        else:
                            await self.log_error(f"download failed: {name} (status {resp.status})")
                            
            except discord.Forbidden:
                await self.log_error(f"forbidden: {name}")
            except discord.HTTPException as e:
                await self.log_error(f"http error: {name} - {e.text[:50]}")
            except Exception as e:
                await self.log_error(f"error: {str(e)[:40]}")
            
            self.completed += 1
        
        self.print_status()
    
    async def guild_edit(self, guild_to, guild_data):
        self.current_operation = "Updating Guild"
        try:
            name = guild_data.get("name", "unnamed")
            await guild_to.edit(name=name)
            await self.log_add(f"renamed guild to: {name}")
            
            icon_hash = guild_data.get("icon")
            if icon_hash:
                try:
                    gid = guild_data.get("id", "0")
                    icon_url = f"https://cdn.discordapp.com/icons/{gid}/{icon_hash}.png"
                    async with aiohttp.ClientSession() as session:
                        async with session.get(icon_url) as resp:
                            if resp.status == 200:
                                icon_image = await resp.read()
                                await guild_to.edit(icon=icon_image)
                                await self.log_add("changed guild icon")
                except Exception as e:
                    console.print(f"[yellow]icon failed: {str(e)[:40]}[/yellow]")
                    
        except discord.Forbidden:
            await self.log_error("forbidden to edit guild")


class CloneBot:
    def __init__(self):
        self.client = None
        self.bot_token = BOT_TOKEN
        self.user_token = USER_TOKEN
        self.source_id = None
        self.target_id = None
        
        self.role_map = {}
        self.chan_map = {}
        self.cat_map = {}
        
        self.stats = {
            "roles": 0,
            "cats": 0,
            "chans": 0,
            "emojis": 0,
            "errors": 0
        }
        
        self.logs = deque(maxlen=30)
        self.running = True
        self.cloner = None
        self.scraper = None
        self.scraped_data = None
        self.auto_login = False
    
    def log(self, msg, lvl="info"):
        t = datetime.now().strftime("%H:%M:%S")
        self.logs.append((t, lvl, msg))
        
        colors = {"info": "blue", "ok": "green", "warn": "yellow", "err": "red"}
        c = colors.get(lvl, "white")
        console.print(f"[{c}][{t}] {msg}[/{c}]")
    
    def clear(self):
        os.system('cls' if os.name == 'nt' else 'clear')
    
    def show_main(self):
        self.clear()
        console.print(BANNER)
        console.print()
        
        console.print(Panel("[bright_blue]Developer: 42saph[/bright_blue]", border_style="dim blue", box=box.SIMPLE))
        console.print()
        
        parts = []
        if self.user_token:
            parts.append("[green]user: ready[/green]")
        else:
            parts.append("[dim]user: not set[/dim]")
        
        if self.client and self.client.user:
            parts.append(f"[blue]bot: {self.client.user}[/blue]")
        else:
            parts.append("[dim]bot: offline[/dim]")
        
        if self.scraped_data:
            parts.append("[green]data: loaded[/green]")
        
        if self.auto_login:
            parts.append("[cyan]auto-login[/cyan]")
        
        console.print(Panel(Align.center(" | ".join(parts)), border_style="blue", box=box.SIMPLE))
        console.print()
        
        left = self.make_menu()
        right = self.make_status()
        
        grid = Table.grid(expand=True)
        grid.add_column(ratio=3)
        grid.add_column(ratio=2)
        grid.add_row(left, right)
        
        console.print(grid)
        console.print()
        self.show_logs()
        console.print()
    
    def make_menu(self):
        tbl = Table(show_header=False, box=box.ROUNDED, border_style="blue", padding=(0, 1))
        tbl.add_column(style="bright_blue", justify="center", width=4)
        tbl.add_column(style="white")
        tbl.add_column(style="dim blue")
        
        if not self.user_token:
            tbl.add_row("1", "set user token", "for scraping source")
        else:
            tbl.add_row("1", "change user token", "ready")
        
        if not self.client:
            tbl.add_row("2", "connect bot", "for target")
        else:
            tbl.add_row("2", "reconnect bot", "online")
        
        tbl.add_row("3", "scrape source", "fetch server data")
        tbl.add_row("4", "set target", "destination server")
        
        tbl.add_row("", "", "")
        tbl.add_row("5", "[bright_blue]FULL CLONE[/bright_blue]", "copy everything")
        tbl.add_row("6", "clone roles only", "roles + perms")
        tbl.add_row("7", "clone structure", "categories + channels")
        tbl.add_row("8", "clone emojis", "custom emojis")
        
        tbl.add_row("", "", "")
        tbl.add_row("d", "delete target roles", "wipe")
        tbl.add_row("w", "delete target channels", "wipe")
        
        tbl.add_row("", "", "")
        tbl.add_row("s", "stats", "session info")
        tbl.add_row("c", "clear", "refresh")
        tbl.add_row("x", "[red]exit[/red]", "quit")
        
        return Panel(tbl, title="[bold blue] menu [/bold blue]", border_style="blue")
    
    def make_status(self):
        tbl = Table(show_header=False, box=box.SIMPLE, border_style="dim blue", padding=(0, 1))
        tbl.add_column(style="blue", width=12)
        tbl.add_column(style="white")
        
        if self.user_token:
            tbl.add_row("user token", "[green]set[/green]")
        else:
            tbl.add_row("user token", "[dim]not set[/dim]")
        
        if self.client and self.client.user:
            tbl.add_row("bot", str(self.client.user))
        else:
            tbl.add_row("bot", "[dim]offline[/dim]")
        
        tbl.add_row("", "")
        
        if self.scraped_data:
            guild = self.scraped_data.get("guild", {})
            if isinstance(guild, dict) and "name" in guild:
                tbl.add_row("source", guild["name"][:24])
                tbl.add_row("source id", str(self.source_id))
                chans = len(self.scraped_data.get("channels", []))
                roles = len(self.scraped_data.get("roles", []))
                emojis = len(self.scraped_data.get("emojis", []))
                tbl.add_row("  channels", str(chans))
                tbl.add_row("  roles", str(roles))
                tbl.add_row("  emojis", str(emojis))
            elif isinstance(guild, dict) and "error" in guild:
                tbl.add_row("source", f"[red]error: {guild['error']}[/red]")
            else:
                tbl.add_row("source", "[yellow]no data[/yellow]")
        elif self.source_id:
            tbl.add_row("source id", str(self.source_id))
        else:
            tbl.add_row("source", "[dim]not set[/dim]")
        
        tbl.add_row("", "")
        
        target = self.get_target()
        if target:
            tbl.add_row("target", target.name[:24])
            tbl.add_row("target id", str(self.target_id))
        elif self.target_id:
            tbl.add_row("target id", str(self.target_id))
            tbl.add_row("status", "[yellow]bot not in server[/yellow]")
        else:
            tbl.add_row("target", "[dim]not set[/dim]")
        
        tbl.add_row("", "")
        tbl.add_row("[bright_blue]stats[/bright_blue]", "")
        tbl.add_row("roles", str(self.stats["roles"]))
        tbl.add_row("categories", str(self.stats["cats"]))
        tbl.add_row("channels", str(self.stats["chans"]))
        tbl.add_row("emojis", str(self.stats["emojis"]))
        
        return Panel(tbl, title="[bold blue] status [/bold blue]", border_style="blue")
    
    def show_logs(self):
        lines = []
        for t, lvl, msg in list(self.logs)[-5:]:
            colors = {"info": "blue", "ok": "green", "warn": "yellow", "err": "red"}
            c = colors.get(lvl, "white")
            lines.append(f"[dim]{t}[/dim] [{c}]{msg}[/{c}]")
        
        text = "\n".join(lines) if lines else "[dim]waiting...[/dim]"
        console.print(Panel(text, title="[bold blue] log [/bold blue]", border_style="blue", height=8))
    
    def get_input(self, prompt):
        console.print(f"[bold blue]{prompt}[/bold blue]", end="")
        return input().strip()
    
    async def run(self):
        has_config = CONFIG_AVAILABLE and self.user_token and self.bot_token
        
        if has_config:
            self.auto_login = True
            self.log("config.py detected with credentials", "ok")
            if not await self.auto_setup():
                self.auto_login = False
                if not await self.manual_setup():
                    return
        else:
            if not await self.manual_setup():
                return
        
        while self.running:
            self.show_main()
            
            choice = self.get_input("select: ").lower()
            
            if choice == "1":
                await self.set_user_token()
            elif choice == "2":
                await self.connect_bot()
            elif choice == "3":
                await self.scrape_source()
            elif choice == "4":
                await self.set_target()
            elif choice == "5":
                await self.do_full_clone()
            elif choice == "6":
                await self.do_clone_roles()
            elif choice == "7":
                await self.do_clone_structure()
            elif choice == "8":
                await self.do_clone_emojis()
            elif choice == "d":
                await self.do_delete_roles()
            elif choice == "w":
                await self.do_delete_channels()
            elif choice == "s":
                self.show_stats()
            elif choice == "c":
                continue
            elif choice == "x":
                self.running = False
        
        await self.cleanup()
    
    async def auto_setup(self):
        self.clear()
        console.print(BANNER)
        console.print()
        console.print(Panel("[bright_blue]sapphirus[/bright_blue] [dim]- auto-login from config[/dim] | [bright_blue]Developer: 42saph[/bright_blue]", border_style="blue", box=box.DOUBLE))
        console.print()
        
        self.scraper = APIScraper(self.user_token)
        await self.scraper.init()
        self.log("user token loaded from config", "ok")
        
        self.log("testing user token...", "info")
        test = await self.scraper.get("/users/@me")
        if test and "id" in test:
            self.log(f"user: {test.get('username')}", "ok")
        else:
            self.log("user token invalid", "err")
            return False
        
        intents = discord.Intents.default()
        intents.guilds = True
        intents.emojis = True
        
        self.client = commands.Bot(command_prefix="!", intents=intents)
        ready = asyncio.Event()
        
        @self.client.event
        async def on_ready():
            self.log(f"bot: {self.client.user}", "ok")
            ready.set()
        
        try:
            asyncio.create_task(self.client.start(self.bot_token, reconnect=False))
            await asyncio.wait_for(ready.wait(), timeout=20)
            self.cloner = Clone(self)
            self.log("auto-login successful", "ok")
            self.get_input("press enter to continue...")
            return True
        except Exception as e:
            self.log(f"bot login failed: {str(e)[:50]}", "err")
            return False
    
    async def manual_setup(self):
        self.clear()
        console.print(BANNER)
        console.print()
        console.print(Panel("[bright_blue]sapphirus[/bright_blue] [dim]- manual setup[/dim] | [bright_blue]Developer: 42saph[/bright_blue]", border_style="blue", box=box.DOUBLE))
        console.print()
        
        if not self.user_token:
            token = self.get_input("enter user token: ")
            if not token:
                console.print("[red]cancelled[/red]")
                return False
            self.user_token = token
        
        self.scraper = APIScraper(self.user_token)
        await self.scraper.init()
        self.log("user token set", "ok")
        
        self.log("testing api access...", "info")
        test = await self.scraper.get("/users/@me")
        if test and "id" in test:
            self.log(f"user: {test.get('username')}", "ok")
        else:
            self.log("token may be invalid", "warn")
        
        if not self.bot_token:
            bot_token = self.get_input("enter bot token: ")
            if not bot_token:
                console.print("[red]cancelled[/red]")
                return False
            self.bot_token = bot_token
        
        intents = discord.Intents.default()
        intents.guilds = True
        intents.emojis = True
        
        self.client = commands.Bot(command_prefix="!", intents=intents)
        ready = asyncio.Event()
        
        @self.client.event
        async def on_ready():
            self.log(f"bot: {self.client.user}", "ok")
            ready.set()
        
        try:
            asyncio.create_task(self.client.start(self.bot_token, reconnect=False))
            await asyncio.wait_for(ready.wait(), timeout=20)
            self.cloner = Clone(self)
            return True
        except Exception as e:
            console.print(f"[red]bot failed: {str(e)[:50]}[/red]")
            time.sleep(2)
            return False
    
    async def cleanup(self):
        if self.client:
            try:
                await self.client.close()
            except:
                pass
        if self.scraper:
            try:
                await self.scraper.close()
            except:
                pass
        self.clear()
        console.print("[blue]goodbye[/blue]")
    
    async def set_user_token(self):
        self.clear()
        console.print(BANNER)
        console.print()
        
        token = self.get_input("enter new user token: ")
        if not token:
            return
        
        if self.scraper:
            await self.scraper.close()
        
        self.user_token = token
        self.scraper = APIScraper(token)
        await self.scraper.init()
        self.log("user token updated", "ok")
        self.get_input("press enter...")
    
    async def connect_bot(self):
        if self.client:
            try:
                await self.client.close()
            except:
                pass
            self.client = None
        
        self.clear()
        console.print(BANNER)
        console.print()
        
        token = self.get_input("enter bot token: ")
        if not token:
            return
        
        self.bot_token = token
        intents = discord.Intents.default()
        intents.guilds = True
        intents.emojis = True
        
        self.client = commands.Bot(command_prefix="!", intents=intents)
        ready = asyncio.Event()
        
        @self.client.event
        async def on_ready():
            self.log(f"bot: {self.client.user}", "ok")
            ready.set()
        
        try:
            asyncio.create_task(self.client.start(self.bot_token, reconnect=False))
            await asyncio.wait_for(ready.wait(), timeout=20)
            self.cloner = Clone(self)
        except Exception as e:
            self.log(f"bot error: {str(e)[:50]}", "err")
            self.get_input("press enter...")
    
    async def scrape_source(self):
        if not self.scraper:
            self.log("no user token", "err")
            return
        
        sid = self.get_input("enter source server id: ")
        try:
            self.source_id = int(sid)
        except:
            self.log("invalid id", "err")
            return
        
        self.log(f"scraping {self.source_id}...", "info")
        
        self.scraped_data = await self.scraper.scrape_server(self.source_id)
        guild = self.scraped_data.get("guild", {})
        
        if not isinstance(guild, dict):
            self.log("invalid response format", "err")
            self.get_input("press enter...")
            return
        
        if "error" in guild:
            self.log(f"error: {guild['error']}", "err")
            if "status" in guild:
                self.log(f"status code: {guild['status']}", "err")
            self.get_input("press enter...")
            return
        
        if "name" not in guild:
            self.log("no guild data returned", "err")
            self.get_input("press enter...")
            return
        
        self.log(f"success: {guild['name']}", "ok")
        chans = len(self.scraped_data.get("channels", []))
        roles = len(self.scraped_data.get("roles", []))
        emojis = len(self.scraped_data.get("emojis", []))
        self.log(f"data: {chans} channels, {roles} roles, {emojis} emojis", "info")
        self.get_input("press enter...")
    
    async def set_target(self):
        if not self.client:
            self.log("connect bot first", "err")
            return
        
        self.clear()
        console.print(BANNER)
        console.print()
        console.print("[bold blue]select target server (bot must be in this server)[/bold blue]")
        console.print()
        
        tbl = Table(show_header=True, header_style="bold blue", box=box.ROUNDED, border_style="blue")
        tbl.add_column("#", style="bright_blue", justify="center")
        tbl.add_column("name", style="white")
        tbl.add_column("id", style="dim")
        tbl.add_column("members", justify="right")
        
        guilds = list(self.client.guilds)
        for i, g in enumerate(guilds[:15], 1):
            tbl.add_row(str(i), g.name[:30], str(g.id), str(g.member_count))
        
        console.print(tbl)
        console.print()
        
        sel = self.get_input("enter number or server id: ")
        
        try:
            num = int(sel)
            if 1 <= num <= len(guilds):
                self.target_id = guilds[num-1].id
                self.log(f"target: {guilds[num-1].name}", "ok")
                self.get_input("press enter...")
                return
        except:
            pass
        
        try:
            sid = int(sel)
            for g in guilds:
                if g.id == sid:
                    self.target_id = sid
                    self.log(f"target: {g.name}", "ok")
                    self.get_input("press enter...")
                    return
        except:
            pass
        
        self.log("invalid selection", "err")
        self.get_input("press enter...")
    
    def get_target(self):
        if not self.client or not self.target_id:
            return None
        return self.client.get_guild(self.target_id)
    
    def check_target(self):
        if not self.client:
            self.log("no bot", "err")
            return None
        if not self.target_id:
            self.log("no target set", "err")
            return None
        
        target = self.client.get_guild(self.target_id)
        if not target:
            self.log("bot not in target server", "err")
            return None
        
        me = target.me
        if not me.guild_permissions.manage_channels or not me.guild_permissions.manage_roles:
            self.log("bot needs manage_channels and manage_roles", "err")
            return None
        
        return target
    
    async def do_full_clone(self):
        if not self.scraped_data:
            self.log("no scraped data", "err")
            return
        
        target = self.check_target()
        if not target:
            return
        
        guild = self.scraped_data.get("guild", {})
        if not isinstance(guild, dict) or "name" not in guild:
            self.log("invalid source data", "err")
            return
        
        source_name = guild.get("name", "unknown")
        
        self.clear()
        console.print(BANNER)
        console.print()
        console.print(f"[bright_blue]FULL CLONE[/bright_blue]")
        console.print(f"[yellow]{source_name} -> {target.name}[/yellow]")
        console.print()
        console.print("this will delete target content and recreate from source")
        console.print()
        
        if self.get_input("type 'clone' to proceed: ") != "clone":
            return
        
        self.role_map = {}
        self.cat_map = {}
        self.chan_map = {}
        
        console.print("\n[bold blue]=== Phase 1: Guild Settings ===[/bold blue]")
        self.cloner.reset_stats()
        await self.cloner.guild_edit(target, guild)
        
        console.print("\n[bold blue]=== Phase 2: Cleanup ===[/bold blue]")
        self.cloner.reset_stats()
        await self.cloner.emojis_delete(target)
        
        self.cloner.reset_stats()
        await self.cloner.channels_delete(target)
        
        self.cloner.reset_stats()
        await self.cloner.roles_delete(target)
        
        console.print("\n[bold blue]=== Phase 3: Creating Roles ===[/bold blue]")
        self.cloner.reset_stats()
        roles_data = self.scraped_data.get("roles", [])
        await self.cloner.roles_create(target, roles_data)
        
        console.print("\n[bold blue]=== Phase 4: Creating Structure ===[/bold blue]")
        channels_data = self.scraped_data.get("channels", [])
        
        self.cloner.reset_stats()
        await self.cloner.categories_create(target, channels_data)
        
        self.cloner.reset_stats()
        await self.cloner.channels_create(target, channels_data)
        
        console.print("\n[bold blue]=== Phase 5: Creating Emojis ===[/bold blue]")
        self.cloner.reset_stats()
        emojis_data = self.scraped_data.get("emojis", [])
        await self.cloner.emojis_create(target, emojis_data)
        
        console.print(f"\n[green]=== Full Clone Completed ===[/green]")
        console.print("[dim]Press Enter to return to main menu...[/dim]")
        input()
    
    async def do_clone_roles(self):
        if not self.scraped_data:
            self.log("no data", "err")
            return
        
        target = self.check_target()
        if not target:
            return
        
        roles_data = self.scraped_data.get("roles", [])
        
        self.clear()
        console.print(BANNER)
        console.print()
        console.print(f"[yellow]clone {len(roles_data)} roles to {target.name}?[/yellow]")
        
        if self.get_input("delete existing first? (y/n): ") == "y":
            self.cloner.reset_stats()
            await self.cloner.roles_delete(target)
        
        self.cloner.reset_stats()
        await self.cloner.roles_create(target, roles_data)
        
        console.print(f"\n[green]Roles cloned successfully![/green]")
        console.print("[dim]Press Enter to return to main menu...[/dim]")
        input()
    
    async def do_clone_structure(self):
        if not self.scraped_data:
            self.log("no data", "err")
            return
        
        target = self.check_target()
        if not target:
            return
        
        channels_data = self.scraped_data.get("channels", [])
        
        self.clear()
        console.print(BANNER)
        console.print()
        console.print(f"[yellow]clone structure to {target.name}?[/yellow]")
        
        if self.get_input("delete existing channels first? (y/n): ") == "y":
            self.cloner.reset_stats()
            await self.cloner.channels_delete(target)
        
        self.cloner.reset_stats()
        await self.cloner.categories_create(target, channels_data)
        
        self.cloner.reset_stats()
        await self.cloner.channels_create(target, channels_data)
        
        console.print(f"\n[green]Structure cloned successfully![/green]")
        console.print("[dim]Press Enter to return to main menu...[/dim]")
        input()
    
    async def do_clone_emojis(self):
        if not self.scraped_data:
            self.log("no data", "err")
            return
        
        target = self.check_target()
        if not target:
            return
        
        emojis_data = self.scraped_data.get("emojis", [])
        
        self.clear()
        console.print(BANNER)
        console.print()
        console.print(f"[yellow]clone {len(emojis_data)} emojis to {target.name}?[/yellow]")
        
        if self.get_input("delete existing first? (y/n): ") == "y":
            self.cloner.reset_stats()
            await self.cloner.emojis_delete(target)
        
        self.cloner.reset_stats()
        await self.cloner.emojis_create(target, emojis_data)
        
        console.print(f"\n[green]Emojis cloned successfully![/green]")
        console.print("[dim]Press Enter to return to main menu...[/dim]")
        input()
    
    async def do_delete_roles(self):
        target = self.check_target()
        if not target:
            return
        
        self.clear()
        console.print(BANNER)
        console.print()
        console.print(f"[red]DELETE ALL ROLES in {target.name}?[/red]")
        if self.get_input("type 'delete' to confirm: ") != "delete":
            return
        
        self.cloner.reset_stats()
        await self.cloner.roles_delete(target)
        
        console.print(f"\n[green]Roles deleted successfully![/green]")
        console.print("[dim]Press Enter to return to main menu...[/dim]")
        input()
    
    async def do_delete_channels(self):
        target = self.check_target()
        if not target:
            return
        
        self.clear()
        console.print(BANNER)
        console.print()
        console.print(f"[red]DELETE ALL CHANNELS in {target.name}?[/red]")
        if self.get_input("type 'delete' to confirm: ") != "delete":
            return
        
        self.cloner.reset_stats()
        await self.cloner.channels_delete(target)
        
        console.print(f"\n[green]Channels deleted successfully![/green]")
        console.print("[dim]Press Enter to return to main menu...[/dim]")
        input()
    
    def show_stats(self):
        self.clear()
        console.print(BANNER)
        console.print()
        
        tbl = Table(title="[bold blue]session statistics[/bold blue]", box=box.DOUBLE, border_style="blue")
        tbl.add_column("metric", style="blue")
        tbl.add_column("value", style="bright_blue", justify="right")
        
        tbl.add_row("roles created", str(self.stats["roles"]))
        tbl.add_row("categories created", str(self.stats["cats"]))
        tbl.add_row("channels created", str(self.stats["chans"]))
        tbl.add_row("emojis created", str(self.stats["emojis"]))
        
        if self.source_id:
            tbl.add_row("", "")
            tbl.add_row("source id", str(self.source_id))
            guild = self.scraped_data.get("guild", {}) if self.scraped_data else {}
            if isinstance(guild, dict) and "name" in guild:
                tbl.add_row("source name", guild["name"])
        
        if self.target_id:
            tbl.add_row("", "")
            tbl.add_row("target id", str(self.target_id))
            target = self.get_target()
            if target:
                tbl.add_row("target name", target.name)
        
        console.print(tbl)
        console.print()
        self.get_input("press enter...")


def run():
    bot = CloneBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        console.print("[blue]\ninterrupted[/blue]")
        sys.exit(0)


if __name__ == "__main__":
    run()