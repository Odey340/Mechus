# Switch VM to Generic Kernel for ALSA (so Mechus can hear)

On Ubuntu 24.04 **AWS** EC2, the default kernel (`6.17.0-1007-aws`) does not include sound modules, so `snd-aloop` is not available and Mechus cannot receive raw audio in Docker. Switching to the **generic** kernel and loading `snd-aloop` can fix this — **but on many EC2 AMIs the generic kernel cannot mount the root filesystem** (missing block drivers in initramfs), leading to kernel panic and an unreachable instance.

---

## ⚠️ EC2 generic kernel often fails (root fs)

On Ubuntu 24.04 **EC2**, the generic kernel (e.g. `6.8.0-101-generic`) may boot but then:

```
VFS: Cannot open root device "PARTUUID=..." or unknown-block(0,0): error -6
Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)
```

The generic kernel’s initramfs does not include the block device drivers this EC2 instance uses for its root disk, so the system never reaches a login and **SSH will not work**. If you already set `GRUB_DEFAULT` to the generic kernel, the instance will keep booting it and panicking. You must **revert to the AWS kernel** (see “Recovery” below) or use a different provider (e.g. Hetzner) where the generic kernel is supported.

---

## Recovery: revert to AWS kernel (instance unreachable)

If the instance is in a panic loop and you cannot SSH in:

1. **EC2 Serial Console** (if enabled): Connect via Serial Console, wait for the next boot, interrupt GRUB (e.g. Esc or hold Shift), choose **Advanced options for Ubuntu** → **Ubuntu, with Linux 6.17.0-1007-aws**. After you get a shell, run:
   ```bash
   sudo sed -i 's/^GRUB_DEFAULT=.*/GRUB_DEFAULT=0/' /etc/default/grub
   sudo update-grub
   sudo reboot
   ```
   so future reboots use the default (AWS) kernel again.

2. **Attach root volume to another instance**: Stop the broken instance, detach its root volume, attach it to a healthy Ubuntu instance, mount it (e.g. under `/mnt`), then:
   ```bash
   sudo sed -i 's/^GRUB_DEFAULT=.*/GRUB_DEFAULT=0/' /mnt/etc/default/grub
   ```
   Unmount, detach the volume, reattach to the original instance, start the instance. It should boot the AWS kernel (default menu entry 0).

After recovery, the instance will be back on the AWS kernel; `snd-aloop` will still be unavailable. Use one of the alternatives in **docs/VOICE_INPUT_ISSUE.md** (e.g. different cloud, or host with real/generic kernel).

---

## Step 1: SSH into the VM

From your Mac (in the mechusdemo repo):

```bash
./run.sh ssh
```

You should see a prompt like `ubuntu@ip-172-31-6-140:~$`.

---

## Step 2: Install the generic kernel and reboot

On the VM, run:

```bash
sudo apt update
sudo apt install -y linux-generic
sudo reboot
```

The VM will restart. Your SSH session will drop (connection reset). Wait about 1–2 minutes.

---

## Step 3: SSH back in and check the kernel

From your Mac again:

```bash
./run.sh ssh
```

Then on the VM:

```bash
uname -r
```

- If you see **`-generic`** (e.g. `6.8.0-xxx-generic`), you are on the generic kernel. Continue to Step 4.
- If you still see **`-aws`** (e.g. `6.17.0-1007-aws`), the VM may have booted the AWS kernel by default. At the GRUB screen you can choose “Advanced options” → pick the **generic** kernel. If you don’t see GRUB (e.g. EC2 console), set the generic kernel as default then reboot again:

  ```bash
  # List kernel menu entries (optional: find "6.8.0-101-generic")
  grep -E 'menuentry |submenu ' /boot/grub/grub.cfg | head -20

  # Set default to Advanced options (1) → entry 2 = 6.8.0-101-generic
  sudo grub-set-default "1>2"
  sudo reboot
  ```

  If you still boot the AWS kernel, run `grep menuentry /boot/grub/grub.cfg` and use `1>N` where N is the 0-based index of the generic kernel inside the "Advanced options" submenu.

  **If EC2 still boots the AWS kernel** (saved_entry is 1>2 but `uname -r` stays `-aws`), the AMI may ignore GRUB’s saved entry. Force the generic kernel via `GRUB_DEFAULT`:

  ```bash
  # List entries (use sudo)
  sudo grep -E 'submenu|menuentry ' /boot/grub/grub.cfg | cat -n

  # Set default by exact menu title (adjust the title if your grub.cfg differs)
  sudo sed -i 's/^GRUB_DEFAULT=.*/GRUB_DEFAULT="Advanced options for Ubuntu>Ubuntu, with Linux 6.8.0-101-generic"/' /etc/default/grub
  sudo update-grub
  sudo reboot
  ```

  After reboot, confirm with `uname -r` (should show `6.8.0-101-generic`).

---

## Step 4: Install extra modules and load snd-aloop

Still on the VM (with generic kernel):

```bash
sudo apt install -y linux-modules-extra-$(uname -r)
sudo modprobe snd-aloop
ls -la /dev/snd
```

- If you see device nodes under `/dev/snd`, ALSA is available. Continue to Step 5.
- If `modprobe snd-aloop` fails or `/dev/snd` is missing, the generic kernel’s extra package may not include it on your image; try another Ubuntu AMI or a different instance type.

---

## Step 5: Make snd-aloop load at boot (optional)

So that after a reboot you still have ALSA without running `modprobe` by hand:

```bash
echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf
```

---

## Step 6: Expose ALSA to the Mechus container

The repo’s `docker-compose.zoom.yml` already includes `devices: ["/dev/snd"]` and `group_add: ["audio"]`. If you deploy **before** Steps 1–4 (generic kernel + snd-aloop), the container may fail to start because `/dev/snd` does not exist on the host. Do the kernel and modprobe steps first, then deploy.

If you prefer to edit only on the VM, add the following under the `mechus` service.

```bash
cd ~/mechusdemo
nano docker-compose.zoom.yml
```

Add under the `mechus` service (same level as `image:`, `build:`, `environment:`), with correct indentation (2 spaces):

```yaml
    devices:
      - /dev/snd
    group_add:
      - audio
```

Save and exit (in nano: Ctrl+O, Enter, Ctrl+X).

If the file is managed from your Mac (you deploy with `./run.sh deploy`), add the same block in your **local** `docker-compose.zoom.yml` in the repo, then deploy so it syncs to the VM (Step 7).

---

## Step 7: Rebuild and restart Mechus on the VM

**Option A — You edited on the VM:**  
On the VM:

```bash
cd ~/mechusdemo
docker-compose -f docker-compose.yml -f docker-compose.zoom.yml down
docker-compose -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
```

**Option B — You edited locally and use deploy:**  
From your Mac:

```bash
./run.sh deploy
```

This syncs the repo (including the updated `docker-compose.zoom.yml`) and runs the restart script on the VM.

---

## Step 8: Verify

1. **Health:** From your Mac: `curl http://3.16.207.181:5001/health` (replace with your VM’s IP). Should return `{"status":"ok",...}`.

2. **Join a meeting** (from the dashboard or API). In the VM logs you should eventually see something like:
   - `CanStartRawRecording returned ...` then SUCCESS (after host allows or with recording token)
   - `raw_recording_started`
   - `setExternalAudioSource result=...`
   - `subscribe_raw_audio_result=...`
   - `onMixedAudioRawDataReceived: first frame...` or `onOneWayAudioRawDataReceived: first frame...`

3. **Stream logs from Mac:**  
   `./run.sh ssh-logs`  
   (or SSH in and run `docker-compose -f docker-compose.yml -f docker-compose.zoom.yml logs -f mechus`).

---

## Summary

| Step | Where   | Action |
|------|--------|--------|
| 1    | Mac    | `./run.sh ssh` |
| 2    | VM     | `sudo apt install -y linux-generic && sudo reboot` |
| 3    | Mac    | Wait 1–2 min, then `./run.sh ssh`; on VM run `uname -r` (expect `-generic`) |
| 4    | VM     | `sudo apt install -y linux-modules-extra-$(uname -r)`, `sudo modprobe snd-aloop`, `ls /dev/snd` |
| 5    | VM     | (Optional) `echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf` |
| 6    | VM or Mac | Add `devices: ["/dev/snd"]` and `group_add: ["audio"]` to `docker-compose.zoom.yml` under `mechus` |
| 7    | VM or Mac | Restart stack (on VM: docker-compose down/up; or from Mac: `./run.sh deploy`) |
| 8    | Mac    | Test join + check logs for “first frame” |

After this, Mechus on the VM can receive Zoom raw audio (assuming recording is allowed or you use a recording token). Host must still allow recording when prompted, or you pass a recording token from your backend.
