import discord
from discord.ext import commands
import asyncio
import os

# ============================================================
# Intents
# ============================================================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True


# ============================================================
# Bot
# ============================================================
class YWPBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Cog読み込み
        cogs = [
            "cogs.ywp_auto",
            "cogs.ywp_commands",
            "cogs.ywp_stages",
            "cogs.ywp_panel",
        ]

        for cog in cogs:
            try:
                await self.load_extension(cog)
                print(f"✅ ロード: {cog}")
            except Exception as e:
                print(f"❌ ロード失敗 {cog}: {e}")

        # スラッシュコマンド同期
        try:
            synced = await self.tree.sync()
            print(f"🔁 スラッシュコマンド同期: {len(synced)}件")
            for cmd in synced:
                print(f"    /{cmd.name}")
        except Exception as e:
            print(f"❌ 同期失敗: {e}")


bot = YWPBot()


# ============================================================
# 起動イベント
# ============================================================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    print(f"📡 接続サーバー: {len(bot.guilds)}件")


# ============================================================
# 起動
# ============================================================
async def main():
    async with bot:
        await bot.start("ここにトークン")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 停止")