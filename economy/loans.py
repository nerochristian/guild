import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import discord
from discord.ext import commands

from components_v2 import branded_panel_container
from .core import send_v2


LOGGER = logging.getLogger("enzo-bot.economy-loans")
LOAN_DURATION = timedelta(days=7)
MAX_BIGINT = 9_223_372_036_854_775_807
MAX_DEBTS_DISPLAYED = 15


def _parse_interest(raw_value: str, principal: int) -> int:
    normalized = raw_value.strip().replace(",", "")
    is_percentage = normalized.endswith("%")
    if is_percentage:
        normalized = normalized[:-1].strip()

    try:
        value = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("Interest must be a whole amount or percentage.") from exc

    if not value.is_finite() or value < 0:
        raise ValueError("Interest cannot be negative or infinite.")

    interest = int(Decimal(principal) * value / 100) if is_percentage else int(value)
    if value != value.to_integral_value() and not is_percentage:
        raise ValueError("Interest must be a whole amount.")
    if interest > MAX_BIGINT - principal:
        raise ValueError("The loan total is too large.")
    return interest


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_percentage(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0%"
    percentage = Decimal(numerator) * 100 / Decimal(denominator)
    formatted = f"{percentage.quantize(Decimal('0.1')):f}".rstrip("0").rstrip(".")
    return f"{formatted}%"


class LoanTermsModal(discord.ui.Modal, title="Loan Terms"):
    interest_input = discord.ui.TextInput(
        label="Interest / Extra Amount",
        style=discord.TextStyle.short,
        placeholder="e.g. 5000 or 10%",
        required=True,
        max_length=32,
    )

    def __init__(self, loan_view: "LoanRequestView"):
        super().__init__()
        self.loan_view = loan_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.loan_view.status not in {"pending", "terms_offered"}:
            return await interaction.response.send_message(
                "This loan request is no longer active.", ephemeral=True
            )
        if interaction.user.id != self.loan_view.lender.id:
            return await interaction.response.send_message(
                f"Only the lender ({self.loan_view.lender.mention}) can offer terms.", ephemeral=True
            )

        try:
            interest = _parse_interest(
                self.interest_input.value, self.loan_view.principal
            )
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)

        self.loan_view.interest = interest
        self.loan_view.status = "terms_offered"
        self.loan_view.render()
        await interaction.response.edit_message(view=self.loan_view)


class LoanRequestView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "LoansCog",
        borrower: discord.Member,
        lender: discord.Member,
        principal: int,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.borrower = borrower
        self.lender = lender
        self.principal = principal
        self.interest = 0
        self.status = "pending"
        self.message: Optional[discord.Message] = None
        self._accept_lock = asyncio.Lock()
        self.render()

    def _description(self) -> str:
        if self.status == "pending":
            return (
                f"{self.borrower.mention} is requesting **{self.principal:,}** from "
                f"{self.lender.mention}.\n\n"
                f"{self.lender.mention}, choose **Offer Terms** to set the interest."
            )
        if self.status == "terms_offered":
            return (
                f"{self.lender.mention} offered these terms to {self.borrower.mention}:\n\n"
                f"Principal: **{self.principal:,}**\n"
                f"Interest: **{self.interest:,}**\n"
                f"Total to repay: **{self.principal + self.interest:,}**\n"
                "Due: **7 days after acceptance**"
            )
        if self.status == "accepted":
            return (
                f"**{self.principal:,}** was transferred to {self.borrower.mention}.\n"
                f"Total to repay to {self.lender.mention}: "
                f"**{self.principal + self.interest:,}** within 7 days."
            )
        if self.status == "cancelled":
            return "This loan request was cancelled."
        if self.status == "expired":
            return "This loan request expired before it was accepted."
        return "This loan request could not be processed."

    def render(self) -> None:
        self.clear_items()
        actions: list[discord.ui.Button[Any]] = []
        if self.status in {"pending", "terms_offered"}:
            offer_button = discord.ui.Button(
                label="Change Terms" if self.status == "terms_offered" else "Offer Terms",
                style=discord.ButtonStyle.primary,
            )
            cancel_button = discord.ui.Button(
                label="Cancel", style=discord.ButtonStyle.danger
            )
            offer_button.callback = self.offer_terms
            cancel_button.callback = self.cancel_loan
            actions.extend((offer_button, cancel_button))

        if self.status == "terms_offered":
            accept_button = discord.ui.Button(
                label="Accept Terms", style=discord.ButtonStyle.success
            )
            accept_button.callback = self.accept_terms
            actions.insert(0, accept_button)

        title_by_status = {
            "pending": "Loan Request",
            "terms_offered": "Loan Terms Offered",
            "accepted": "Loan Accepted",
            "cancelled": "Loan Cancelled",
            "expired": "Loan Request Expired",
            "failed": "Loan Failed",
        }
        color_by_status = {
            "accepted": 0x2ECC71,
            "cancelled": 0xE74C3C,
            "expired": 0x6B7280,
            "failed": 0xE74C3C,
        }
        container = branded_panel_container(
            title=title_by_status[self.status],
            description=self._description(),
            accent_color=color_by_status.get(self.status, 0xF1C40F),
            actions=actions or None,
        )
        self.add_item(container)

    async def offer_terms(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.lender.id:
            return await interaction.response.send_message(
                f"Only the lender ({self.lender.mention}) can offer terms.", ephemeral=True
            )
        if self.status not in {"pending", "terms_offered"}:
            return await interaction.response.send_message(
                "This loan request is no longer active.", ephemeral=True
            )

        lender_total = await asyncio.to_thread(
            self.cog.store.get_available_funds,
            interaction.guild_id,
            self.lender.id,
        )
        if lender_total < self.principal:
            return await interaction.response.send_message(
                "You no longer have enough funds to provide this loan.", ephemeral=True
            )
        await interaction.response.send_modal(LoanTermsModal(self))

    async def accept_terms(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.borrower.id:
            return await interaction.response.send_message(
                f"Only the borrower ({self.borrower.mention}) can accept these terms.", ephemeral=True
            )

        await interaction.response.defer()
        async with self._accept_lock:
            if self.status != "terms_offered":
                return await interaction.followup.send(
                    "This loan request is no longer awaiting acceptance.", ephemeral=True
                )

            success = await asyncio.to_thread(
                self.cog.store.execute_loan,
                interaction.guild_id,
                self.borrower.id,
                self.lender.id,
                self.principal,
                self.interest,
            )
            if not success:
                return await interaction.followup.send(
                    "The loan could not be processed. The lender may no longer have enough funds.",
                    ephemeral=True,
                )

            self.status = "accepted"
            self.render()
            self.stop()
            await interaction.edit_original_response(view=self)

    async def cancel_loan(self, interaction: discord.Interaction) -> None:
        if interaction.user.id not in (self.borrower.id, self.lender.id):
            return await interaction.response.send_message(
                f"Only the borrower ({self.borrower.mention}) or lender ({self.lender.mention}) can cancel this request.", ephemeral=True
            )
        if self.status not in {"pending", "terms_offered"}:
            return await interaction.response.send_message(
                "This loan request is no longer active.", ephemeral=True
            )

        self.status = "cancelled"
        self.render()
        self.stop()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        if self.status not in {"pending", "terms_offered"}:
            return
        self.status = "expired"
        self.render()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.HTTPException, discord.NotFound):
                pass


class LoansStoreMixin:
    def get_available_funds(self, guild_id: int, user_id: int) -> int:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT bank, credits FROM member_economy WHERE guild_id = %s AND user_id = %s",
                    (guild_id, user_id),
                )
                row = cursor.fetchone()
                return (int(row["bank"]) + int(row["credits"])) if row else 0
        finally:
            self._pool.putconn(conn)

    def execute_loan(
        self,
        guild_id: int,
        borrower_id: int,
        lender_id: int,
        principal: int,
        interest: int,
    ) -> bool:
        if (
            borrower_id == lender_id
            or principal <= 0
            or interest < 0
            or principal > MAX_BIGINT
            or interest > MAX_BIGINT - principal
        ):
            return False

        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT bank, credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, lender_id),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return False

                    bank = int(row["bank"])
                    credits = int(row["credits"])
                    if bank + credits < principal:
                        return False

                    deduct_bank = min(bank, principal)
                    deduct_credits = principal - deduct_bank
                    cursor.execute(
                        """
                        UPDATE member_economy
                        SET bank = bank - %s,
                            credits = credits - %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (deduct_bank, deduct_credits, guild_id, lender_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, bank, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            bank = member_economy.bank + EXCLUDED.bank,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (guild_id, borrower_id, principal),
                    )
                    cursor.execute(
                        """
                        INSERT INTO economy_loans
                            (guild_id, lender_id, borrower_id, principal, interest, due_at, status)
                        VALUES (%s, %s, %s, %s, %s, %s, 'active')
                        """,
                        (
                            guild_id,
                            lender_id,
                            borrower_id,
                            principal,
                            interest,
                            datetime.now(timezone.utc) + LOAN_DURATION,
                        ),
                    )
                    return True
        except Exception:
            LOGGER.exception("Failed to execute loan")
            return False
        finally:
            self._pool.putconn(conn)

    def get_loans(self, guild_id: int, borrower_id: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT * FROM economy_loans
                    WHERE guild_id = %s AND borrower_id = %s AND status = 'active'
                    ORDER BY due_at ASC, id ASC
                    """,
                    (guild_id, borrower_id),
                )
                return [dict(row) for row in cursor.fetchall()]
        finally:
            self._pool.putconn(conn)

    def repay_loan(
        self,
        guild_id: int,
        borrower_id: int,
        loan_id: int,
        amount: Optional[int] = None,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT * FROM economy_loans
                        WHERE id = %s AND guild_id = %s AND borrower_id = %s
                            AND status = 'active'
                        FOR UPDATE
                        """,
                        (loan_id, guild_id, borrower_id),
                    )
                    loan = cursor.fetchone()
                    if not loan:
                        return {"ok": False, "reason": "not_found"}

                    total_owed = int(loan["principal"]) + int(loan["interest"])
                    amount_paid = min(max(0, int(loan["amount_paid"])), total_owed)
                    remaining = total_owed - amount_paid
                    if remaining <= 0:
                        cursor.execute(
                            "UPDATE economy_loans SET status = 'paid' WHERE id = %s",
                            (loan_id,),
                        )
                        return {"ok": False, "reason": "not_found"}

                    if amount is not None and amount <= 0:
                        return {"ok": False, "reason": "invalid"}

                    payment = remaining if amount is None else min(amount, remaining)
                    lender_id = int(loan["lender_id"])
                    cursor.execute(
                        "SELECT bank, credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, borrower_id),
                    )
                    row = cursor.fetchone()
                    bank = int(row["bank"]) if row else 0
                    credits = int(row["credits"]) if row else 0
                    if bank + credits < payment:
                        return {
                            "ok": False,
                            "reason": "poor",
                            "owed": payment,
                            "balance": bank + credits,
                        }

                    deduct_bank = min(bank, payment)
                    deduct_credits = payment - deduct_bank
                    cursor.execute(
                        """
                        UPDATE member_economy
                        SET bank = bank - %s,
                            credits = credits - %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (deduct_bank, deduct_credits, guild_id, borrower_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, bank, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            bank = member_economy.bank + EXCLUDED.bank,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (guild_id, lender_id, payment),
                    )
                    total_paid = amount_paid + payment
                    remaining = total_owed - total_paid
                    cursor.execute(
                        """
                        UPDATE economy_loans
                        SET amount_paid = %s,
                            status = CASE WHEN %s >= principal + interest THEN 'paid' ELSE 'active' END
                        WHERE id = %s
                        """,
                        (total_paid, total_paid, loan_id),
                    )
                    return {
                        "ok": True,
                        "paid": payment,
                        "total_paid": total_paid,
                        "total": total_owed,
                        "remaining": remaining,
                        "complete": remaining == 0,
                    }
        except Exception:
            LOGGER.exception("Failed to repay loan %s", loan_id)
            return {"ok": False, "reason": "error"}
        finally:
            self._pool.putconn(conn)


class LoansCog(commands.Cog, name="Loans"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    async def loan(self, ctx: commands.Context) -> None:
        await ctx.send(
            "Usage: `.loan request @user <amount>`, `.loan list`, or "
            "`.loan payback <loan_id> [amount]`"
        )

    @loan.command(name="request")
    async def loan_request(
        self, ctx: commands.Context, member: discord.Member, amount: int
    ) -> None:
        if amount <= 0:
            return await ctx.send("Amount must be greater than zero.")
        if amount > MAX_BIGINT:
            return await ctx.send("That loan amount is too large.")
        if member.id == ctx.author.id:
            return await ctx.send("You cannot request a loan from yourself.")
        if member.bot:
            return await ctx.send("You cannot request a loan from a bot.")

        lender_total = await asyncio.to_thread(
            self.store.get_available_funds,
            ctx.guild.id,
            member.id,
        )
        if lender_total < amount:
            return await ctx.send(f"{member.mention} does not have enough funds to provide a loan of **{amount:,}**.")

        view = LoanRequestView(self, ctx.author, member, amount)
        view.message = await send_v2(ctx, embed=None, view=view)

    @loan.command(name="payback", aliases=["pay"])
    async def loan_payback(
        self, ctx: commands.Context, loan_id: int, amount: Optional[int] = None
    ) -> None:
        result = await asyncio.to_thread(
            self.store.repay_loan, ctx.guild.id, ctx.author.id, loan_id, amount
        )
        if not result["ok"]:
            if result["reason"] == "invalid":
                return await ctx.send("Payment amount must be greater than zero.")
            if result["reason"] == "poor":
                return await ctx.send(
                    f"You need **{result['owed']:,}** for this payment but only have "
                    f"**{result['balance']:,}** available."
                )
            if result["reason"] == "not_found":
                return await ctx.send(
                    "Active loan not found, or that loan does not belong to you."
                )
            return await ctx.send("The loan could not be repaid. Please try again.")

        paid_percentage = _format_percentage(result["total_paid"], result["total"])
        container = branded_panel_container(
            title="Loan Repaid" if result["complete"] else "Loan Payment Sent",
            description=(
                f"Paid **{result['paid']:,}** toward loan #{loan_id}.\n"
                f"Total paid: **{result['total_paid']:,}** of **{result['total']:,}** "
                f"(**{paid_percentage}**)\n"
                f"Remaining: **{result['remaining']:,}**"
            ),
            accent_color=0x2ECC71,
        )
        _view = discord.ui.LayoutView()
        _view.add_item(container)
        await send_v2(ctx, embed=None, view=_view)

    @loan.command(name="list")
    async def loan_list(self, ctx: commands.Context) -> None:
        loans = await asyncio.to_thread(
            self.store.get_loans, ctx.guild.id, ctx.author.id
        )
        if not loans:
            return await ctx.send("You don't have any active loans.")

        lines = []
        for loan in loans:
            total = int(loan["principal"]) + int(loan["interest"])
            due_at = int(_as_utc(loan["due_at"]).timestamp())
            lines.append(
                f"**ID:** {loan['id']} | **Lender:** <@{loan['lender_id']}> | "
                f"**Owe:** {total:,} | **Due:** <t:{due_at}:R>"
            )

        container = branded_panel_container(
            title="Your Active Loans",
            description="\n".join(lines),
            accent_color=0x3498DB,
        )
        _view = discord.ui.LayoutView()
        _view.add_item(container)
        await send_v2(ctx, embed=None, view=_view)

    @commands.command(name="debt", aliases=["debts"])
    @commands.guild_only()
    async def debt(self, ctx: commands.Context) -> None:
        loans, debt_info = await asyncio.gather(
            asyncio.to_thread(self.store.get_loans, ctx.guild.id, ctx.author.id),
            asyncio.to_thread(self.store.get_debt_info, ctx.guild.id, ctx.author.id),
        )
        economy_debt, debt_deadline = debt_info
        economy_debt = int(economy_debt)

        if not loans and economy_debt <= 0:
            return await ctx.send("You do not owe any active debts.")

        sections = []
        for loan in loans[:MAX_DEBTS_DISPLAYED]:
            principal = int(loan["principal"])
            interest = int(loan["interest"])
            total = principal + interest
            paid = min(max(0, int(loan["amount_paid"])), total)
            remaining = total - paid
            due_at = int(_as_utc(loan["due_at"]).timestamp())
            sections.append(
                f"**Loan #{loan['id']} — owe <@{loan['lender_id']}>**\n"
                f"Principal: **{principal:,}** | Interest: **{interest:,}** "
                f"(**{_format_percentage(interest, principal)}**)\n"
                f"Paid: **{paid:,}** of **{total:,}** "
                f"(**{_format_percentage(paid, total)}**)\n"
                f"Remaining: **{remaining:,}** "
                f"(**{_format_percentage(remaining, total)}**) | Due <t:{due_at}:R>"
            )

        hidden_loans = len(loans) - MAX_DEBTS_DISPLAYED
        if hidden_loans > 0:
            sections.append(
                f"*{hidden_loans} more active loan{'s' if hidden_loans != 1 else ''} "
                "not shown.*"
            )

        if economy_debt > 0:
            deadline = (
                f" | Due <t:{int(_as_utc(debt_deadline).timestamp())}:R>"
                if debt_deadline
                else ""
            )
            sections.append(
                "**Economy debt — owe the guild economy**\n"
                f"Remaining: **{economy_debt:,}**{deadline}\n"
                "Use `.paydebt [amount]` to pay it."
            )

        container = branded_panel_container(
            title="Your Debts",
            description=(
                "\n\n".join(sections)
                + "\n\nUse `.loan payback <loan_id> [amount]` to repay a loan."
            ),
            accent_color=0xE67E22,
        )
        view = discord.ui.LayoutView()
        view.add_item(container)
        await send_v2(ctx, embed=None, view=view)

    @commands.command(name="paydebt")
    @commands.guild_only()
    async def paydebt(self, ctx: commands.Context, amount: Optional[int] = None) -> None:
        result = await asyncio.to_thread(
            self._pay_debt, ctx.guild.id, ctx.author.id, amount
        )
        if result["reason"] == "none":
            return await ctx.send("You do not have any debt.")
        if result["reason"] == "invalid":
            return await ctx.send("Amount must be positive.")
        if result["reason"] == "poor":
            return await ctx.send(
                f"You don't have enough funds to pay **{result['amount']:,}**."
            )

        container = branded_panel_container(
            title="Debt Paid",
            description=(
                f"You paid **{result['paid']:,}** of your debt.\n"
                f"Remaining debt: **{result['remaining']:,}**"
            ),
            accent_color=0x2ECC71,
        )
        _view = discord.ui.LayoutView()
        _view.add_item(container)
        await send_v2(ctx, embed=None, view=_view)

    def _pay_debt(
        self, guild_id: int, user_id: int, amount: Optional[int]
    ) -> dict[str, Any]:
        conn = self.store._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT debt, bank, credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    if not row or int(row["debt"]) <= 0:
                        return {"reason": "none"}

                    debt = int(row["debt"])
                    bank = int(row["bank"])
                    credits = int(row["credits"])
                    pay_amount = debt if amount is None else amount
                    if pay_amount <= 0:
                        return {"reason": "invalid"}

                    pay_amount = min(pay_amount, debt)
                    if bank + credits < pay_amount:
                        return {"reason": "poor", "amount": pay_amount}

                    deduct_bank = min(bank, pay_amount)
                    deduct_credits = pay_amount - deduct_bank
                    cursor.execute(
                        """
                        UPDATE member_economy
                        SET bank = bank - %s,
                            credits = credits - %s,
                            debt = debt - %s,
                            debt_deadline = CASE
                                WHEN debt - %s <= 0 THEN NULL ELSE debt_deadline
                            END,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (
                            deduct_bank,
                            deduct_credits,
                            pay_amount,
                            pay_amount,
                            guild_id,
                            user_id,
                        ),
                    )
                    return {
                        "reason": "ok",
                        "paid": pay_amount,
                        "remaining": debt - pay_amount,
                    }
        finally:
            self.store._pool.putconn(conn)
