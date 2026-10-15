// Atom Lite -> Govee H617E on/off toggle over BLE.
//
// Press the button (G39): connect to the strip, ask it whether it is on
// (0xAA 0x01 on 2b11, reply on 2b10), then send the opposite power packet
// (0x33 0x01 0x01 on / 0x33 0x01 0x00 off). If the strip never answers the
// query, toggle from the last state we sent, kept in flash (NVS).
//
// LED (SK6812, G27): blue = working, green = now on, dim white = now off,
// red = could not reach the strip.
#include <Arduino.h>
#include <BLEDevice.h>
#include <Preferences.h>

static const char *STRIP_MAC = "ee:2e:01:06:53:0e";  // Govee_H617E_530E
static BLEUUID SERVICE("00010203-0405-0607-0809-0a0b0c0d1910");
static BLEUUID WRITE_CHAR("00010203-0405-0607-0809-0a0b0c0d2b11");
static BLEUUID NOTIFY_CHAR("00010203-0405-0607-0809-0a0b0c0d2b10");
static const int LED_PIN = 27;
static const int BUTTON_PIN = 39;  // active low, external pull-up on the Atom
static const int ATTEMPTS = 3;

static Preferences prefs;
// One client for the life of the program. Creating and deleting a BLEClient per
// press crashed the ESP32 BLE stack on the second delete (xQueueGenericSend /
// spinlock asserts), rebooting the Atom after every other toggle.
static BLEClient *client;
static volatile int reportedPower = -1;  // -1 = no answer yet

static void led(uint8_t r, uint8_t g, uint8_t b) { neopixelWrite(LED_PIN, r, g, b); }

// 20 bytes: 19 command bytes, then XOR of those 19.
static void packet(uint8_t *out, uint8_t a, uint8_t b, uint8_t c) {
  memset(out, 0, 20);
  out[0] = a; out[1] = b; out[2] = c;
  uint8_t x = 0;
  for (int i = 0; i < 19; i++) x ^= out[i];
  out[19] = x;
}

static void onNotify(BLERemoteCharacteristic *, uint8_t *data, size_t len, bool) {
  if (len >= 3 && data[0] == 0xAA && data[1] == 0x01) reportedPower = data[2] ? 1 : 0;
}

// Returns the new power state (0/1), or -1 if the strip could not be reached.
static int toggleOnce(int remembered) {
  BLEAddress addr(STRIP_MAC);
  bool ok = client->connect(addr, BLE_ADDR_TYPE_RANDOM) || client->connect(addr, BLE_ADDR_TYPE_PUBLIC);
  if (!ok) return -1;

  int result = -1;
  BLERemoteService *svc = client->getService(SERVICE);
  BLERemoteCharacteristic *w = svc ? svc->getCharacteristic(WRITE_CHAR) : nullptr;
  if (w) {
    int current = remembered;
    BLERemoteCharacteristic *n = svc->getCharacteristic(NOTIFY_CHAR);
    if (n && n->canNotify()) {
      reportedPower = -1;
      n->registerForNotify(onNotify);
      uint8_t q[20];
      packet(q, 0xAA, 0x01, 0x00);
      w->writeValue(q, 20, false);
      for (int i = 0; i < 20 && reportedPower < 0; i++) delay(25);  // up to 500 ms
      if (reportedPower >= 0) current = reportedPower;
      Serial.printf("query: %s\n", reportedPower >= 0 ? (reportedPower ? "on" : "off") : "no answer, using memory");
    }
    int target = current ? 0 : 1;
    uint8_t p[20];
    packet(p, 0x33, 0x01, (uint8_t)target);
    w->writeValue(p, 20, false);
    delay(100);  // let the write leave before we drop the link
    result = target;
  }
  client->disconnect();
  return result;
}

void setup() {
  Serial.begin(115200);
  pinMode(BUTTON_PIN, INPUT);
  prefs.begin("govee", false);
  BLEDevice::init("atom-govee-switch");
  client = BLEDevice::createClient();
  led(0, 0, 0);
  Serial.printf("atom-govee-switch up, last sent: %s\n", prefs.getBool("on", false) ? "on" : "off");
}

void loop() {
  static bool wasDown = false;
  bool down = digitalRead(BUTTON_PIN) == LOW;
  if (down && !wasDown) {
    delay(30);  // debounce
    if (digitalRead(BUTTON_PIN) == LOW) {
      led(0, 0, 40);
      int remembered = prefs.getBool("on", false) ? 1 : 0;
      int state = -1;
      for (int i = 0; i < ATTEMPTS && state < 0; i++) {
        state = toggleOnce(remembered);
        if (state < 0) { Serial.printf("attempt %d failed\n", i + 1); delay(300); }
      }
      if (state < 0) {
        led(40, 0, 0);
      } else {
        prefs.putBool("on", state == 1);
        Serial.printf("strip now %s\n", state ? "on" : "off");
        state ? led(0, 40, 0) : led(6, 6, 6);
      }
      delay(1500);
      led(0, 0, 0);
    }
  }
  wasDown = down;
  delay(10);
}
