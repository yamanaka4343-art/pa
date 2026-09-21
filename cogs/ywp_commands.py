import discord
from discord import app_commands, ui
from discord.ext import commands
import asyncio
import json
import os
import time
import random
from datetime import datetime

from cogs.ywp_auto import Client, login_email, RC
from cogs.ywp_panel import run_farm

# ============================================================
# データファイル
# ============================================================
DATA_DIR = "data"
SESSION_FILE = f"{DATA_DIR}/sessions.json"
LOOP_FILE = f"{DATA_DIR}/loops.json"


def ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_json(path, default=None):
    ensure_dir()
    if not os.path.exists(path):
        return default or {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return default or {}


def save_json(path, data):
    ensure_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_sessions() -> dict:
    return load_json(SESSION_FILE, {})


def save_sessions(data: dict):
    save_json(SESSION_FILE, data)


def load_loops() -> dict:
    return load_json(LOOP_FILE, {})


def save_loops(data: dict):
    save_json(LOOP_FILE, data)


# ============================================================
# 3秒問題対策ヘルパー
# ============================================================
async def safe_defer(interaction: discord.Interaction, ephemeral: bool = True, thinking: bool = True) -> bool:
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
        return True
    except discord.NotFound:
        return False
    except discord.HTTPException:
        return False


async def safe_reply(interaction: discord.Interaction, **kwargs):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs)
        else:
            return await interaction.response.send_message(**kwargs)
    except discord.NotFound:
        pass
    except discord.HTTPException:
        pass


async def safe_edit(interaction: discord.Interaction, **kwargs):
    try:
        return await interaction.edit_original_response(**kwargs)
    except discord.NotFound:
        pass
    except discord.HTTPException:
        pass


# ============================================================
# ログインモーダル
# ============================================================
class LoginModal(ui.Modal, title="ぷにぷに ログイン"):
    email_input = ui.TextInput(
        label="メールアドレス",
        placeholder="example@mail.com",
        required=True,
        max_length=200
    )
    password_input = ui.TextInput(
        label="パスワード",
        placeholder="パスワードを入力",
        required=True,
        max_length=200
    )

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return

        email = self.email_input.value.strip()
        password = self.password_input.value

        try:
            await interaction.followup.send(
                "🔐 **ログイン処理中...**\n"
                "UDkey取得 → メール連携 → ゲームサーバーログイン を実行します。\n"
                "（10〜20秒かかります）",
                ephemeral=True
            )
        except Exception:
            return

        try:
            client = await asyncio.to_thread(login_email, email, password)
        except Exception as e:
            await safe_edit(
                interaction,
                content=f"❌ **ログイン失敗**\n```{str(e)[:300]}```"
            )
            return

        sessions = load_sessions()
        uid = str(interaction.user.id)
        sessions[uid] = {
            "udkey": client.udkey,
            "gdkey": client.gdkey,
            "userId": client.userId,
            "token": client.token,
            "mst": client.mst,
            "saved_at": datetime.now().isoformat(),
            "playerName": client.info().get("playerName", "不明")
        }
        save_sessions(sessions)

        self.password_input = None

        info = client.info()
        embed = discord.Embed(
            title="✅ ログイン成功",
            color=0x00ff88,
            timestamp=datetime.now()
        )
        embed.add_field(name="👤 プレイヤー名", value=info.get("playerName", "不明"), inline=True)
        embed.add_field(name="🆔 ユーザーID", value=str(client.userId), inline=True)
        embed.add_field(name="🔑 UDkey", value=f"`{client.udkey[:20]}...`", inline=False)
        embed.set_footer(text="次は /ywp_farm で周回開始")

        await safe_edit(interaction, content=None, embed=embed)


# ============================================================
# 周回タスク
# ============================================================
# ============================================================
# Cog
# ============================================================
class YWPCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ログイン", description="ぷにぷにアカウントにログイン（モーダル）")
    async def ywp_login(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_modal(LoginModal(self.bot))
        except discord.NotFound:
            pass
        except discord.HTTPException:
            pass

    @app_commands.command(name="周回", description="ステージを自動周回")
    @app_commands.describe(
        stage="ステージID（例: 288051）",
        count="周回回数",
        request_delay="リクエスト前待機秒（デフォルト0.5）",
        cooldown="クールダウン秒（デフォルト3.0）"
    )
    async def ywp_farm(
        self,
        interaction: discord.Interaction,
        stage: int,
        count: int = 10,
        request_delay: float = 0.5,
        cooldown: float = 3.0
    ):
        if not await safe_defer(interaction):
            return

        sessions = load_sessions()
        sdata = sessions.get(str(interaction.user.id))
        if not sdata:
            return await safe_reply(
                interaction,
                content="❌ セッションがありません。先に `/ywp_login` を実行してください。",
                ephemeral=True
            )

        embed = discord.Embed(
            title="🚀 周回を開始します",
            description=(
                f"**ステージ**: `{stage}`\n"
                f"**回数**: {count}回\n"
                f"**待機**: {request_delay}秒\n"
                f"**CD**: {cooldown}秒"
            ),
            color=0x5865F2
        )
        await safe_reply(interaction, embed=embed, ephemeral=True)

        # 指定された待機秒をそのまま使う（未指定ならパネル同様のランダム待機）
        asyncio.create_task(run_farm(
            self.bot,
            interaction,
            interaction.guild_id,
            interaction.user.id,
            stage,
            count,
            request_delay,
            cooldown,
            None,
            False,
            False
        ))

    @app_commands.command(name="状態", description="ログイン状態を確認")
    async def ywp_status(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return

        sessions = load_sessions()
        sdata = sessions.get(str(interaction.user.id))
        if not sdata:
            return await safe_reply(
                interaction,
                content="❌ ログインしていません。",
                ephemeral=True
            )

        embed = discord.Embed(
            title="📊 ログイン状態",
            color=0x00ff88,
            timestamp=datetime.now()
        )
        embed.add_field(name="👤 プレイヤー名", value=sdata.get("playerName", "不明"), inline=True)
        embed.add_field(name="🆔 ユーザーID", value=str(sdata.get("userId")), inline=True)
        embed.add_field(name="🔑 UDkey", value=f"`{str(sdata.get('udkey'))[:20]}...`", inline=False)
        embed.add_field(name="📅 保存日時", value=sdata.get("saved_at", "不明"), inline=False)
        await safe_reply(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="停止", description="実行中の周回を停止")
    async def ywp_stop(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return

        loops = load_loops()
        count = 0
        for key, data in loops.items():
            if data.get("user_id") == interaction.user.id and data.get("status") == "running":
                data["status"] = "stopped"
                count += 1
        save_loops(loops)

        await safe_reply(
            interaction,
            content=f"🛑 {count}件の周回を停止しました。",
            ephemeral=True
        )

    @app_commands.command(name="ログアウト", description="セッションを削除")
    async def ywp_logout(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return

        sessions = load_sessions()
        uid = str(interaction.user.id)
        if uid in sessions:
            del sessions[uid]
            save_sessions(sessions)
            await safe_reply(interaction, content="✅ ログアウトしました。", ephemeral=True)
        else:
            await safe_reply(interaction, content="⚠️ ログインしていません。", ephemeral=True)


# ============================================================
# setup
# ============================================================
async def setup(bot: commands.Bot):
    await bot.add_cog(YWPCommands(bot))