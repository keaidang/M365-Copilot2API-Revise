package web

import (
	"strings"
	"testing"
)

// Upstream replies below are captured verbatim from production logs
// (journalctl -u m365-copilot2api, 2026-09-23 image-gen-debug entries).
func TestIsMissingAttachmentRefusal(t *testing.T) {
	refusals := []string{
		"Please upload the image you want edited. I don't currently have any image attachment available in this chat.",
		"Please upload the image you want edited. I don't have any attached image available in this chat.",
		"请上传要编辑的图片，我这边没有收到任何附件。",
	}
	for _, s := range refusals {
		if !isMissingAttachmentRefusal(s) {
			t.Errorf("isMissingAttachmentRefusal(%q) = false, want true", s)
		}
	}
	notRefusals := []string{
		"",
		"抱歉，图片生成请求未能通过安全检查，因此无法返回图片 URL。",
		"Here is the edited image: https://example.com/a.png",
	}
	for _, s := range notRefusals {
		if isMissingAttachmentRefusal(s) {
			t.Errorf("isMissingAttachmentRefusal(%q) = true, want false", s)
		}
	}
}

func TestImageNoResourceMessage(t *testing.T) {
	const fallback = "upstream returned no image resource"
	if got := imageNoResourceMessage(""); got != fallback {
		t.Errorf("empty text: got %q, want %q", got, fallback)
	}
	if got := imageNoResourceMessage("   "); got != fallback {
		t.Errorf("blank text: got %q, want %q", got, fallback)
	}
	got := imageNoResourceMessage("  抱歉，图片生成请求未能通过安全检查。  ")
	want := fallback + ": 抱歉，图片生成请求未能通过安全检查。"
	if got != want {
		t.Errorf("got %q, want %q", got, want)
	}
	long := imageNoResourceMessage(strings.Repeat("图", 600))
	body := strings.TrimPrefix(long, fallback+": ")
	if n := len([]rune(body)); n > 501 { // 500 runes + ellipsis
		t.Errorf("long text not truncated: %d runes", n)
	}
}
