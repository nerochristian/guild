import asyncio
from .core import CoreStoreMixin, CoreCog
from .pets import PetsStoreMixin, PetsCog
from .casino import CasinoStoreMixin, CasinoCog
from .crime import CrimeStoreMixin, CrimeCog
from .shop import ShopStoreMixin, ShopCog
from .trading import TradingStoreMixin, TradingCog
from .earning import EarningStoreMixin, EarningCog
from .admin import AdminStoreMixin, AdminCog
from .loans import LoansStoreMixin, LoansCog

class EconomyStore(CoreStoreMixin, PetsStoreMixin, CasinoStoreMixin, CrimeStoreMixin, ShopStoreMixin, TradingStoreMixin, EarningStoreMixin, AdminStoreMixin, LoansStoreMixin):
    pass

class EconomySystem:
    def __init__(self, bot, db_url):
        self.bot = bot
        self.store = EconomyStore(db_url)
        self.color = 0x2B2D31

        self.core_cog = CoreCog(self.bot)
        self.pets_cog = PetsCog(self.bot)
        self.cogs = [
            self.core_cog,
            self.pets_cog,
            CasinoCog(self.bot),
            CrimeCog(self.bot),
            ShopCog(self.bot),
            TradingCog(self.bot),
            EarningCog(self.bot),
            AdminCog(self.bot),
            LoansCog(self.bot)
        ]
        self._wire_cogs()

    def _wire_cogs(self):
        shared_helpers = (
            "create_embed",
            "_add_credits_or_reset_cooldown",
            "_audit_target_user_id",
            "_check_cooldown",
            "_ensure_cooldown_ready",
            "_get_cooldown_reduction",
            "_active_luck_rate",
            "_luck_triggers",
            "_pet_bonus_note",
            "_pet_bonus_result",
            "_reset_cooldown",
            "_send",
            "_simple_panel_view",
            "_start_cooldown",
            "_validate_bet",
        )
        pet_helpers = (
            "_pet_adjusted_gambling_winnings",
            "_pet_luck_bonus",
            "_pet_luck_triggers",
        )
        for helper_name in pet_helpers:
            if not hasattr(self.core_cog, helper_name):
                setattr(self.core_cog, helper_name, getattr(self.pets_cog, helper_name))

        for cog in self.cogs:
            cog.bot = self.bot
            cog.color = self.color
            if cog is self.core_cog:
                continue
            cog.store = self.store
            for helper_name in shared_helpers:
                if not hasattr(cog, helper_name):
                    setattr(cog, helper_name, getattr(self.core_cog, helper_name))
            for helper_name in pet_helpers:
                if not hasattr(cog, helper_name):
                    setattr(cog, helper_name, getattr(self.pets_cog, helper_name))

    async def setup(self):
        # Initialize db
        await asyncio.to_thread(self.store.initialize)
        # Register cogs
        for cog in self.cogs:
            await self.bot.add_cog(cog)

def init_economy_system(bot, db_url):
    return EconomySystem(bot, db_url)

async def setup(bot):
    pass
