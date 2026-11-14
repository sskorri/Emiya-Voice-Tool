# Emiya Voice for Windows

Copy the entire **Emiya Voice** folder to a writable local drive. Open **Emiya Voice.exe**. Close the app before moving its folder. The distribution includes its own 64-bit Python, desktop libraries and CUDA-enabled PyTorch. Windows needs an NVIDIA driver compatible with the CUDA 12.4 build for GPU generation and a working Microsoft Edge WebView2 Runtime for the native window. Discord and the VB-CABLE audio driver are separate Windows installations. The owner installs VB-CABLE and selects CABLE Output as Discord's input when setting up voice chat.

The target gaming PC has an **RTX 3070 Ti with 8 GB**. Saved WAV playback needs no loaded model. Select **Free VRAM** after preparing lines if the game needs the memory. The full package contains the three Chatterbox models on disk. Only a selected model is loaded for generation. Actual frame time and speech latency during a game need a trial on that PC.

## Use Speak in Discord

In **Emiya Voice.exe**, open **Settings → Voice Chat**, select **CABLE Input**, enable **Send speech to calls** and leave **Preview first** selected. In Discord, choose **CABLE Output** as the input device and your regular headphones as the output. In **Speak**, generate a line and choose **Send**. You can also send a saved line from **Takes** with the model unloaded. **Stop Sending** ends playback. Keep the Windows playback and recording defaults on your normal devices. Open **Settings → Effects** to change Background Effects or Reduce Motion.

After installing VB-CABLE on the gaming PC, open PowerShell in the Emiya Voice folder and run the silent format check.

```powershell
.\runtime\python.exe -m ui.kit.sound_out
```

Under `playback`, find **CABLE Input**. At least one of `float32` or `pcm16` should be `true` for 24,000 Hz mono. The probe reports the Windows default routes under `defaults`. If the endpoint is missing, install VB-CABLE on the sending PC, reboot and click **Refresh** in Voice Chat. [VB-CABLE driver and installation instructions](https://vb-audio.com/Cable/)

## Test a complete conversation

1. **Make a local line.** Open **Speak**, select a voice, type “Ready when you are” and click **Generate** (or the button naming the model to load). If the library is empty, first drop a reference clip into **Forge**, name the voice, confirm consent and complete the forge. Play the finished line in **Takes**.
2. **Test the cable alone.** In **Settings → Voice Chat**, choose **CABLE Input**, enable sending, select **Preview first**, set **Start after** to **0 s** and turn **Hear It** off. In Discord's **Voice & Video**, select **CABLE Output** for input and your headphones for output. Start **Mic Test → Let’s Check**. In **Takes**, click the saved line's **Send**. You should hear Discord play the line back once. Clicking the row's ordinary play control should leave Discord's input meter quiet. A moving **CABLE Output** meter in Windows Recording devices with a quiet Discord meter points to Discord's input selection or voice settings. [Discord mic test](https://support.discord.com/hc/en-us/articles/360020641332-Mic-Testing)
3. **Test with a listener.** Stop the mic test and join a private call. Generate a different short line. With **Preview first**, the listener should hear silence until you click **Send**, then hear the words once. Next, choose **Models → Free VRAM** and send the earlier saved line. It should sound the same, and Models should still show no resident model.
4. **Test while gaming.** Set the optional global **Send** and **Stop Sending** shortcuts in **Settings → Keys**, then select a take. With the game in front, send a longer saved line and stop it partway through. If using Discord push-to-talk, set a short **Start after** delay and hold your Discord talk key before playback starts. Check that the listener hears the first word and that the game keeps focus.

On the 8 GB RTX 3070 Ti, repeat the same short gameplay section with the app closed, with a saved take sending after **Free VRAM**, and with one new line generating. Keep game settings the same. Watch the game's frame-time graph or 1% low FPS if available, note any visible hitch, and time the wait from Generate until the listener hears the first word. The saved-line and live-generation results answer different questions.

To check the remaining call settings, use two distinct lines in the private call.

| Setting | What the listener should hear |
| --- | --- |
| Switch on Speak | Generating with the Speak switch off stays private. With it on, the new line sends. |
| Send at once | Generating a line sends it on completion. Generate 3 sends the first completed take from the batch. |
| Wait its turn / Cut to new take | Send the second line during the first. **Wait** plays both in order. **Cut** interrupts the first. **Stop Sending** also clears waiting sends. |
| Hear It / Start after | **Hear It** changes what you hear locally. The listener still hears one copy. **Start after** delays the outgoing line by the selected interval. |

Restore your usual microphone in Discord after testing. Choosing **CABLE Output** as Discord's input routes the app's audio to the call.

## Folder layout

| Folder or file | Contents |
| --- | --- |
| `ui`, `engine`, `gui.py`, `config.py` | Application code loaded at runtime. Keep these beside `Emiya Voice.exe`. |
| `runtime` | CPython, standard library, dependencies, wheel notices and binary libraries |
| `models` | Local Hugging Face snapshots and Chinese tokenizer data |
| `voices/reference` | Managed reference clips. The optional **Use sample** button reads `sample.wav` here. |
| `cache/registry.json`, `cache/<backend>` | Registered voices and their matching .pt conditioning files |
| `outputs/takes`, `ui_settings.json` | Saved take WAVs, their index and app preferences |
| `portable-data`, `.nicegui`, `logs` | Dependency caches, temporary WebView2 profile, framework storage and logs |
| `licenses`, `LICENSE`, `portable-build.json` | Chatterbox and application notices, package versions and included model revisions |

The development checkout and its `dist/Emiya Voice` folder each use their own library. To use a clip from the checkout's `voices/reference` in the portable app, drop it into **Forge**, name the voice and complete the forge. That registers the voice in the portable library. Emiya Voice reads its own registry and does not scan parent folders for voices.

Keep the default output folder for a library that travels with the app. Exports, externally selected output folders and externally referenced clips follow the paths you choose. Windows manages its drivers, WebView2 installation, event records, antivirus, shell history and Recycle Bin. Browser mode opens your separately installed browser and uses that browser's profile. The normal desktop launcher directs app-managed caches and temporary files into this folder. A fresh build excludes the developer's voice library, generated takes, settings and login tokens. Forge a reference clip you have permission to use.

## Verify or rebuild

From PowerShell in the distribution, run `Start-Process -Wait -FilePath '.\Emiya Voice.exe' -ArgumentList '--portable-check'`. Read `logs\portable-check.json`. This imports the packaged app and all three backend modules, checks local model snapshot lookup and checks the private Python and cache paths without opening a window or loading a model.

To share a clean copy with a friend, run the following from the development checkout and send the resulting folder.

```powershell
.\venv\Scripts\python.exe tools\build_portable.py --out ".\dist\Emiya Voice - clean"
```

The builder copies the installed versions from the project's Python 3.11 x64 venv and public model files from its populated local HF and pkuseg caches. It includes the current app and locally installed Chatterbox. Choose a new destination each time. The default output is `dist\Emiya Voice`. Use `--hub` and `--pkuseg` for other cache locations or `--without-models` for a smaller build that requires separate model provisioning. The full package works offline. Keep the included license files and upstream notices when copying it. Model revisions and library versions are listed in `portable-build.json`.

The public model cards identify the model licenses. The runtime retains the license texts supplied by Python and the installed wheels. [Chatterbox model card](https://huggingface.co/ResembleAI/chatterbox) · [Turbo model card](https://huggingface.co/ResembleAI/chatterbox-turbo) · [Windows Python distribution](https://docs.python.org/3.11/using/windows.html) · [WebView2 data folders](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/user-data-folder)
