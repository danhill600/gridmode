def loading_block_positions(width, height, image_height, label_height, gap=18, margin=16):
    block_height = image_height + gap + label_height
    top = max((height - block_height) // 2, margin)
    image_y = top + image_height // 2
    text_y = min(top + image_height + gap + label_height // 2, height - margin)
    return image_y, text_y
