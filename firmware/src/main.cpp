/*
 * Claude Max-plan usage display
 * STM32F103 "Blue Pill" + 0.96" SSD1306 128x64 I2C OLED.
 *
 * The PC (pc/claude_usage.py) does all the work and sends one newline-
 * terminated line per update over USART1:
 *
 *     <textline1>|<textline2>|...|<barPercent>\n
 *
 * Every '|'-separated field except the LAST is drawn as a text row (top to
 * bottom); the last field is an integer 0-100 rendered as a progress bar.
 * This keeps the firmware dumb: change what's shown by editing Python only.
 *
 * On boot it ALSO scans the I2C bus and reports over USART1 (115200), and
 * auto-detects the SSD1306 at 0x3C or 0x3D. Open a serial reader on the same
 * port to see the scan -- this is how we diagnose a blank screen.
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

// Full-buffer hardware-I2C SSD1306. Default Wire = I2C1 (PB6/PB7) on Blue Pill.
U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, /*reset=*/U8X8_PIN_NONE);

// USART1 on PA9/PA10 carries data from the PC (and our diagnostics out).
HardwareSerial DataSerial(PA10, PA9);  // (RX, TX)

static const uint32_t BAUD = 115200;
static const uint32_t STALE_MS = 8000;   // show "waiting" if no packet this long
static const uint32_t HB_MS = 2000;      // heartbeat cadence while waiting

static const uint8_t MAX_LINES = 5;
static char lineBuf[160];
static uint8_t lineLen = 0;

static char textLines[MAX_LINES][32];
static uint8_t textCount = 0;
static int barPct = 0;
static bool haveData = false;
static uint32_t lastPacketMs = 0;
static uint32_t lastHbMs = 0;
static uint8_t oledAddr = 0;             // 0 = not found

static uint8_t scanForOled() {
  const uint8_t candidates[2] = {0x3C, 0x3D};
  for (uint8_t i = 0; i < 2; i++) {
    Wire.beginTransmission(candidates[i]);
    if (Wire.endTransmission() == 0) return candidates[i];
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
  if (!found) DataSerial.print(" (no devices -> check OLED wiring/power)");
  DataSerial.println();
}

static void drawWaiting() {
  if (!oledAddr) return;
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x12_tr);
  u8g2.drawStr(8, 22, "Claude usage");
  u8g2.drawStr(12, 40, "Waiting for PC...");
  u8g2.sendBuffer();
}

static void drawStatus() {
  if (!oledAddr) return;
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x12_tr);

  uint8_t rows = textCount;
  if (rows > 4) rows = 4;
  for (uint8_t i = 0; i < rows; i++) {
    u8g2.drawStr(2, 11 + i * 12, textLines[i]);
  }

  const int bx = 2, by = 54, bw = 124, bh = 9;
  u8g2.drawFrame(bx, by, bw, bh);
  int fill = (barPct * (bw - 2)) / 100;
  if (fill < 0) fill = 0;
  if (fill > bw - 2) fill = bw - 2;
  if (fill > 0) u8g2.drawBox(bx + 1, by + 1, fill, bh - 2);

  u8g2.sendBuffer();
}

static void parsePacket(char *buf) {
  textCount = 0;
  barPct = 0;

  char *fields[MAX_LINES + 2];
  uint8_t n = 0;
  char *p = buf;
  fields[n++] = p;
  while (*p && n < (uint8_t)(MAX_LINES + 2)) {
    if (*p == '|') {
      *p = '\0';
      fields[n++] = p + 1;
    }
    p++;
  }
  if (n == 0) return;

  barPct = atoi(fields[n - 1]);

  uint8_t rows = n - 1;
  if (rows > MAX_LINES) rows = MAX_LINES;
  for (uint8_t i = 0; i < rows; i++) {
    strncpy(textLines[i], fields[i], sizeof(textLines[i]) - 1);
    textLines[i][sizeof(textLines[i]) - 1] = '\0';
  }
  textCount = rows;

  haveData = true;
  lastPacketMs = millis();
}

void setup() {
  DataSerial.begin(BAUD);
  delay(50);
  DataSerial.println();
  DataSerial.println("=== Claude OLED firmware booted ===");

  // Explicit I2C1 pins, then scan + auto-detect the panel.
  Wire.setSCL(PB6);
  Wire.setSDA(PB7);
  Wire.begin();
  Wire.setClock(400000);
  i2cScanReport();
  oledAddr = scanForOled();
  DataSerial.print("OLED detected at: ");
  if (oledAddr) {
    DataSerial.print("0x");
    DataSerial.println(oledAddr, HEX);
    u8g2.setI2CAddress(oledAddr << 1);   // U8g2 wants the 8-bit address
    u8g2.begin();
    u8g2.setBusClock(400000);
    drawWaiting();
    DataSerial.println("Display initialised. Waiting for PC data...");
  } else {
    DataSerial.println("NONE (no SSD1306 on the bus)");
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

  uint32_t now = millis();
  if (haveData && (now - lastPacketMs) < STALE_MS) {
    drawStatus();
  } else {
    drawWaiting();
    // Heartbeat over serial so we can confirm the chip is alive while idle.
    if (now - lastHbMs >= HB_MS) {
      lastHbMs = now;
      DataSerial.print("[hb] alive, oled=");
      if (oledAddr) { DataSerial.print("0x"); DataSerial.println(oledAddr, HEX); }
      else DataSerial.println("none");
    }
  }
  delay(20);
}
