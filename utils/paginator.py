"""
Paginator for embeds
"""

import discord
from discord import ui
from typing import List

class PaginatorView(ui.View):
    def __init__(self, pages: List[discord.Embed], author_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.author_id = author_id
        self.current_page = 0
        self.update_buttons()
    
    def update_buttons(self):
        self.first_button.disabled = self.current_page == 0
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == len(self.pages) - 1
        self.last_button.disabled = self.current_page == len(self.pages) - 1
        self.page_counter.label = f"{self.current_page + 1}/{len(self.pages)}"
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user. id != self.author_id:
            await interaction.response.send_message(
                "This paginator isn't yours!", ephemeral=True
            )
            return False
        return True
    
    @ui.button(label="≪", style=discord.ButtonStyle. secondary)
    async def first_button(self, interaction: discord. Interaction, button: ui.Button):
        self.current_page = 0
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)
    
    @ui.button(label="◀", style=discord.ButtonStyle.primary)
    async def prev_button(self, interaction: discord. Interaction, button: ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)
    
    @ui.button(label="1/1", style=discord.ButtonStyle. secondary, disabled=True)
    async def page_counter(self, interaction: discord.Interaction, button: ui. Button):
        pass
    
    @ui.button(label="▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)
    
    @ui.button(label="≫", style=discord.ButtonStyle.secondary)
    async def last_button(self, interaction: discord.Interaction, button: ui. Button):
        self.current_page = len(self.pages) - 1
        self.update_buttons()
        await interaction. response.edit_message(embed=self.pages[self.current_page], view=self)

class Paginator: 
    @staticmethod
    async def paginate(interaction: discord.Interaction, pages: List[discord.Embed]):
        if len(pages) == 1:
            await interaction.response.send_message(embed=pages[0])
        else:
            view = PaginatorView(pages, interaction.user.id)
            await interaction.response.send_message(embed=pages[0], view=view)