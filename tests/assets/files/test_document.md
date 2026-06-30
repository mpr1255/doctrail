# Test Markdown Document

This is a **test markdown document** for verifying that `.md` files are processed correctly.

## Features Being Tested

- Direct text reading
- Markdown formatting preservation
- Multi-language support: 测试中文字符
- Special characters and emojis: 🚀 📄 ✅

## Expected Behavior

The extraction method should be `direct_text_read` and the content should be:
1. Read directly from the file
2. Stored in a single database row
3. Preserve the markdown formatting as-is

```bash
# This code block should also be preserved
echo "Hello World"
```

> This blockquote should remain intact as well.