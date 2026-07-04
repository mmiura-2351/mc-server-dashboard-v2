// Command stub-geyser stands in for Geyser's RakNet listener inside a
// Minecraft server container, for the Bedrock relay e2e (epic #1540, issue
// #1547). It answers only RakNet's Unconnected Ping (0x01) with an Unconnected
// Pong (0x1c), echoing back the ping's time field -- enough to prove datagrams
// reach the container and a reply routes back through the same flow, without
// booting a real Geyser (a Modrinth/GeyserMC download would make CI flaky;
// real Geyser+Floodgate behavior was already validated live, epic #1540 issue
// #1542). See docs/app/BEDROCK.md and worker/test/e2e/bedrock_e2e_test.go,
// which drives the real bedrocktunnel.Manager and a real container running
// this image against the real relay listener.
//
// RakNet offline-message framing (minecraft.wiki/w/RakNet):
//
//	Unconnected Ping (client -> server): 1 (id) + 8 (time) + 16 (magic) + 8 (client GUID)
//	Unconnected Pong (server -> client): 1 (id) + 8 (time, echoed) + 8 (server GUID) + 16 (magic) + 2 (string length) + N (server id string)
package main

import (
	"encoding/binary"
	"log"
	"net"
	"os"
	"os/signal"
	"syscall"
)

const (
	idUnconnectedPing = 0x01
	idUnconnectedPong = 0x1c
	pingLen           = 1 + 8 + 16 + 8 // id + time + magic + client guid
	geyserPort        = 19132
	serverGUID        = int64(0x1122334455667788)
)

// raknetMagic is RakNet's fixed offline-message magic sequence.
var raknetMagic = [16]byte{0x00, 0xff, 0xff, 0x00, 0xfe, 0xfe, 0xfe, 0xfe, 0xfd, 0xfd, 0xfd, 0xfd, 0x12, 0x34, 0x56, 0x78}

func main() {
	conn, err := net.ListenUDP("udp", &net.UDPAddr{Port: geyserPort})
	if err != nil {
		log.Fatalf("stub-geyser: listen :%d: %v", geyserPort, err)
	}
	defer func() { _ = conn.Close() }()
	log.Printf("stub-geyser: listening on :%d/udp", geyserPort)

	// PID 1 in a container's PID namespace does not get the default terminate
	// action for an unhandled SIGTERM (only an explicit handler does), so
	// without this `docker stop` would wait out the full SIGKILL grace period on
	// every teardown (mirrors worker/test/e2e/stub/java's explicit trap).
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM)
	go func() {
		<-sigCh
		_ = conn.Close()
		os.Exit(0)
	}()

	buf := make([]byte, 2048)
	for {
		n, addr, err := conn.ReadFromUDP(buf)
		if err != nil {
			return // closed (SIGTERM) or a fatal socket error either way.
		}
		if n < pingLen || buf[0] != idUnconnectedPing {
			continue // not an Unconnected Ping: ignore, matching RakNet's silent drop of junk.
		}
		pingTime := buf[1:9]

		reply := make([]byte, 0, 1+8+8+16+2)
		reply = append(reply, idUnconnectedPong)
		reply = append(reply, pingTime...)
		var guidBuf [8]byte
		binary.BigEndian.PutUint64(guidBuf[:], uint64(serverGUID))
		reply = append(reply, guidBuf[:]...)
		reply = append(reply, raknetMagic[:]...)
		motd := []byte("mcsd-stub-geyser")
		var lenBuf [2]byte
		binary.BigEndian.PutUint16(lenBuf[:], uint16(len(motd)))
		reply = append(reply, lenBuf[:]...)
		reply = append(reply, motd...)

		if _, err := conn.WriteToUDP(reply, addr); err != nil {
			log.Printf("stub-geyser: write to %s: %v", addr, err)
		}
	}
}
