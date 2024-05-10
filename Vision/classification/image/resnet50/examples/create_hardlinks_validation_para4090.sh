#!/bin/bash

# 设置源目录和目标目录
SOURCE_DIR="/data/public/ImageNet/ImageNet_TFRecords"
TARGET_DIR="/HOME/scw6cab/run/OCCL/ImageNet/validation"

# 确保目标目录存在
mkdir -p "$TARGET_DIR"

# 遍历源目录中的文件并创建硬链接
for FILE in "$SOURCE_DIR"/validation-[0-9][0-9][0-9][0-9][0-9]-of-00128; do
  if [ -f "$FILE" ]; then
    # 提取文件名中的数字部分
    BASENAME=$(basename "$FILE")
    DIGITS=$(echo "$BASENAME" | grep -oP '(?<=validation-)[0-9]{5}(?=-of-00128)')
    
    # 构建硬链接名称
    LINK_NAME="part-$DIGITS"
    
    # 创建硬链接
    cp "$FILE" "$TARGET_DIR/$LINK_NAME"
    
    echo "Created hard link: $TARGET_DIR/$LINK_NAME -> $FILE"
  fi
done
