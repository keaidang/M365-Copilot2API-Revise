package chathub

import (
	"strings"
	"testing"
)

// The inline image data URI is embedded twice in the invoke
// (message.attachments[].url + the legacy imageBase64 copy). Upstream closes
// the WebSocket with code 1000 before completion once the message grows past
// ~4 MB (observed 2026-09-28: payload 4.63 MB failed 8/8, 3.81 MB worked).
// Large images must drop the legacy copy; small images keep the historical
// payload byte-for-byte.
func TestChatPayloadLargeInlineImageDropsLegacyBase64(t *testing.T) {
	big := strings.Repeat("A", 3_000_000)
	req := Request{
		Text: "Edit the first attached image with GPT Image 2.",
		Attachments: []Attachment{{
			Type:     "image",
			URL:      "data:image/png;base64," + big,
			Name:     "big.png",
			MimeType: "image/png",
		}},
	}
	payload := chatPayload(req, "req-large", true)
	if strings.Contains(payload, `"imageBase64"`) {
		t.Error("large inline image must drop the legacy imageBase64 copy")
	}
	if !strings.Contains(payload, `"attachments"`) {
		t.Error("large inline image must keep the attachments array")
	}
	if len(payload) >= maxInlinePayloadBytes {
		t.Errorf("payload %d still exceeds budget %d", len(payload), maxInlinePayloadBytes)
	}
}

func TestChatPayloadSmallInlineImageKeepsBothCopies(t *testing.T) {
	req := Request{
		Text: "Edit the first attached image with GPT Image 2.",
		Attachments: []Attachment{{
			Type:     "image",
			URL:      "data:image/png;base64,AAAA",
			Name:     "small.png",
			MimeType: "image/png",
		}},
	}
	payload := chatPayload(req, "req-small", true)
	if !strings.Contains(payload, `"imageBase64"`) {
		t.Error("small inline image must keep the legacy imageBase64 copy")
	}
	if !strings.Contains(payload, `"attachments"`) {
		t.Error("small inline image must keep the attachments array")
	}
}
