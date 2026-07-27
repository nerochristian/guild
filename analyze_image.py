from PIL import Image

img = Image.open(r"c:\Users\Dell\Soul\guild\profile-preview.png")
print("Size:", img.size)

img_rgba = img.convert("RGBA")
width, height = img.size
rightmost = 0
for x in range(width):
    for y in range(height):
        if img_rgba.getpixel((x, y))[3] > 0:
            rightmost = max(rightmost, x)

print("Rightmost non-transparent pixel:", rightmost)
