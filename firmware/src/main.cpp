/*
 * VibeGauge firmware — STM32F103 "Blue Pill" + 0.96" SSD1306 128x64 I2C OLED.
 *
 * Protocol (from pc/vibegauge_app.py, one packet per update, newline-terminated):
 *
 *     fh_pct|fh_reset|wk_pct|wk_reset|HH:MM\n
 *
 *   fh_pct   – 5-hour utilisation 0-100 (integer)
 *   fh_reset – time until 5-hour window resets, e.g. "3h05m"
 *   wk_pct   – 7-day utilisation 0-100 (integer)
 *   wk_reset – time until 7-day window resets, e.g. "9h45m"
 *   HH:MM    – wall-clock time from the PC
 *
 * Display layout (128x64, u8g2_font_6x12_tr):
 *
 *   y=10   "23:14  5s ago"
 *   y=30   "5h  26%  in 3h05m"
 *   y=50   "Wk   3%  in 9h45m"
 *   y=55   [progress bar, h=8]
 *
 * --- Wiring ---------------------------------------------------------------
 *  OLED (I2C1):   VCC -> 3V3      GND -> GND
 *                 SCL -> PB6      SDA -> PB7
 *
 *  Data link (USART1 <-> USB-TTL adapter, common GND with the board):
 *                 PA9  (TX) -> adapter RX
 *                 PA10 (RX) -> adapter TX
 *                 GND       -> adapter GND
 */

#include <Arduino.h>
#include <U8g2lib.h>
#include <Wire.h>

U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, /*reset=*/U8X8_PIN_NONE);

HardwareSerial DataSerial(PA10, PA9);  // (RX, TX)

static const uint32_t BAUD   = 115200;
static const uint32_t HB_MS  = 2000;

static char lineBuf[128];
static uint8_t lineLen = 0;

// Parsed packet fields
static int  fhPct  = 0;
static char fhReset[12] = "";
static int  wkPct  = 0;
static char wkReset[12] = "";
static char sentTime[8]  = "";   // "HH:MM" from PC

static bool     haveData    = false;
static uint32_t lastPacketMs = 0;
static uint32_t lastHbMs    = 0;
static uint8_t  oledAddr    = 0;

// ── I2C / OLED helpers ────────────────────────────────────────────────────────

static uint8_t scanForOled() {
  const uint8_t cands[2] = {0x3C, 0x3D};
  for (uint8_t i = 0; i < 2; i++) {
    Wire.beginTransmission(cands[i]);
    if (Wire.endTransmission() == 0) return cands[i];
  }
  return 0;
}

static void i2cScanReport() {
  DataSerial.print("I2C scan:");
  uint8_t found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      DataSerial.print(" 0x");
      if (a < 16) DataSerial.print('0');
      DataSerial.print(a, HEX);
      found++;
    }
  }
  if (!found) DataSerial.print(" (none)");
  DataSerial.println();
}

// ── Display ───────────────────────────────────────────────────────────────────

static void drawWaiting() {
  if (!oledAddr) return;
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x12_tr);
  u8g2.drawStr(30, 35, "VibeGauge");
  u8g2.sendBuffer();
}

static void drawStatus() {
  if (!oledAddr) return;
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x12_tr);

  // Row 0 (y=10): "HH:MM  Xs ago"
  char header[28];
  uint32_t secs = (millis() - lastPacketMs) / 1000;
  if (secs < 60)
    snprintf(header, sizeof(header), "%s  %ds ago", sentTime, (int)secs);
  else if (secs < 3600)
    snprintf(header, sizeof(header), "%s  %dm ago", sentTime, (int)(secs / 60));
  else
    snprintf(header, sizeof(header), "%s  %dh ago", sentTime, (int)(secs / 3600));
  u8g2.drawStr(2, 10, header);

  // Row 1 (y=30): "5h  26%  in 3h05m"
  char line1[32];
  snprintf(line1, sizeof(line1), "5h %3d%%  in %s", fhPct, fhReset);
  u8g2.drawStr(2, 30, line1);

  // Row 2 (y=50): "Wk   3%  in 9h45m"
  char line2[32];
  snprintf(line2, sizeof(line2), "Wk %3d%%  in %s", wkPct, wkReset);
  u8g2.drawStr(2, 50, line2);

  // Progress bar (5h usage), y=55 h=8
  const int bx = 2, by = 55, bw = 124, bh = 8;
  u8g2.drawFrame(bx, by, bw, bh);
  int fill = (fhPct * (bw - 2)) / 100;
  if (fill < 0) fill = 0;
  if (fill > bw - 2) fill = bw - 2;
  if (fill > 0) u8g2.drawBox(bx + 1, by + 1, fill, bh - 2);

  u8g2.sendBuffer();
}

// ── Packet parser ─────────────────────────────────────────────────────────────

static void parsePacket(char *buf) {
  // Expected: fh_pct|fh_reset|wk_pct|wk_reset|HH:MM
  char *f[6];
  uint8_t n = 0;
  char *p = buf;
  f[n++] = p;
  while (*p && n < 6) {
    if (*p == '|') { *p = '\0'; f[n++] = p + 1; }
    p++;
  }
  if (n < 5) return;

  fhPct = atoi(f[0]);
  strncpy(fhReset,  f[1], sizeof(fhReset)  - 1); fhReset[sizeof(fhReset)   - 1] = '\0';
  wkPct = atoi(f[2]);
  strncpy(wkReset,  f[3], sizeof(wkReset)  - 1); wkReset[sizeof(wkReset)   - 1] = '\0';
  strncpy(sentTime, f[4], sizeof(sentTime) - 1); sentTime[sizeof(sentTime) - 1] = '\0';

  haveData    = true;
  lastPacketMs = millis();
}

// ── Arduino entry points ──────────────────────────────────────────────────────

void setup() {
  DataSerial.begin(BAUD);
  delay(50);
  DataSerial.println();
  DataSerial.println("=== VibeGauge firmware booted ===");

  Wire.setSCL(PB6);
  Wire.setSDA(PB7);
  Wire.begin();
  Wire.setClock(400000);
  i2cScanReport();
  oledAddr = scanForOled();
  DataSerial.print("OLED at: ");
  if (oledAddr) {
    DataSerial.print("0x"); DataSerial.println(oledAddr, HEX);
    u8g2.setI2CAddress(oledAddr << 1);
    u8g2.begin();
    u8g2.setBusClock(400000);
    drawWaiting();
  } else {
    DataSerial.println("none");
  }
}

void loop() {
  while (DataSerial.available() > 0) {
    char c = (char)DataSerial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      if (lineLen > 0) parsePacket(lineBuf);
      lineLen = 0;
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    } else {
      lineLen = 0;
    }
  }

  if (haveData) {
    drawStatus();
  } else {
    drawWaiting();
    uint32_t now = millis();
    if (now - lastHbMs >= HB_MS) {
      lastHbMs = now;
      DataSerial.print("[hb] alive, oled=");
      if (oledAddr) { DataSerial.print("0x"); DataSerial.println(oledAddr, HEX); }
      else DataSerial.println("none");
    }
  }
  delay(20);
}
