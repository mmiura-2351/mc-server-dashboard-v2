// Command stub-geyser answers RakNet Unconnected Ping with Pong to test datagram routing without a real Geyser
// download.
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

	// Handle SIGTERM explicitly as container PID 1 so docker stop need not wait for SIGKILL.
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
