# 🟢 Sacred Zonai Realms
## Tears of the Kingdom v1.2.1 Save Converter — All-In-One

**Current Version:** v8.30  
**Developer:** ThyHeroOfTime / Legendary Savage Gamer  
**Target Game Version:** Tears of the Kingdom v1.2.1  
**Platform:** Windows

---

# ⚠️ PLEASE BACK UP YOUR SAVE FIRST

Before using **ANY** save conversion, editing, recovery, hosting, or
replacement feature in Sacred Zonai Realms:

## BACK UP YOUR ORIGINAL SAVE.

**DO NOT convert your only copy of `progress.sav`.**

**DO NOT edit your only original save.**

**DO NOT replace a server save without having a backup.**

Make a copy and keep the untouched original somewhere safe, such as:

- A separate backup folder on your computer
- Your SD card
- An external drive
- Another safe storage location

A good rule to remember:

    ORIGINAL SAVE
         |
         +----> SAFE BACKUP — DO NOT TOUCH
         |
         +----> WORKING COPY — CONVERT / EDIT / TEST

Always work with the **COPY**.

---

# 🌿 What Is Sacred Zonai Realms?

Sacred Zonai Realms is an All-In-One Windows application built around
working with **Tears of the Kingdom v1.2.1 save data and multiplayer
server workflows**.

The project started around making save conversion easier, but over time
it grew into something much larger.

Instead of having a collection of separate scripts, utilities, save
tools, server folders, and troubleshooting programs scattered around
your computer, the goal is to bring the tools together into one
organized application.

The All-In-One Edition currently includes:

- 🔄 KTML → SAV conversion
- 🔄 SAV → KTML conversion
- ✏️ Client Save Editor
- 🗺️ World Position Map
- 🌐 Zonai Realms Hosting tools
- 👥 Traveler / player save management
- 🛠️ Recovery Tools
- 🎮 Multiplayer Toolkit
- 🩺 Diagnostics & Error Reports
- 🎨 Custom wallpapers
- 🔊 Custom interface sounds
- 📜 Credits and community information

This is the larger, feature-rich edition of Sacred Zonai Realms.

---

# 🔄 KTML ↔ SAV Conversion

One of the main features is two-way conversion between server-oriented
KTML data and the game's `progress.sav` format.

### KTML → SAV

Convert compatible `.ktml` save data into:

    progress.sav

### SAV → KTML

Convert:

    progress.sav

into compatible KTML data for supported server workflows.

The converter performs validation instead of simply changing a file
extension.

**A `.sav` file and a `.ktml` file are NOT interchangeable just because
their filenames are changed.**

---

# ✏️ Client Save Editor

Sacred Zonai Realms includes an integrated Client Save Editor so
supported save information can be inspected and edited without having
to constantly jump between completely separate applications.

As always:

### EDIT A COPY — NOT YOUR ONLY ORIGINAL SAVE.

After making changes, test the resulting save before considering it
your new permanent save.

---

# 🌐 Zonai Realms Hosting

The All-In-One Edition also contains tools intended to make working
with a multiplayer realm/server easier.

This includes management of realm-related operations and player save
workflows.

One of the most important rules is:

## PAUSE YOUR REALM BEFORE REPLACING OR UPLOADING LIVE SAVE DATA.

Do not intentionally replace a save while the server is actively
writing to it.

A safer workflow is:

1. Back up your original save.
2. Pause the realm.
3. Download/export the save.
4. Make another working copy.
5. Convert or edit the working copy.
6. Upload/replace the intended save.
7. Allow validation to complete.
8. Confirm the replacement succeeded.
9. Resume the realm.
10. Join the game and verify everything works.

---

# 👥 Traveler / Player Save Management

Sacred Zonai Realms supports working with individual traveler/player
save data.

This is important because a multiplayer server's individual player
data is not necessarily identical to a complete `progress.sav`.

You should **NOT** take a full `progress.sav`, rename it `.ktml`, and
drop it into a server's users directory.

The All-In-One workflow is designed to handle supported player save
data more carefully.

Features include:

- `.KTML` player-save support
- `.SAV` player-save support
- UID-safe player placement
- Automatic backup before replacement
- Save validation
- Atomic replacement behavior
- Rollback protection if replacement fails

The player's real UID is preserved rather than trusting whatever
filename somebody uploaded.

---

# 🗺️ World Position Map

The World Position Map provides a visual way to work with supported
world/player-position information.

This is intended to make coordinate information easier to understand
than staring at raw save values.

---

# 🛠️ Recovery Tools

Mistakes happen.

Save files can be edited incorrectly, conversion can fail, a server can
be interrupted, or somebody can simply select the wrong file.

The Recovery Tools section exists to provide additional options for
diagnosing and recovering from supported save problems.

Recovery tools are **NOT a replacement for making backups**.

Your untouched original save is still your best recovery option.

---

# 🎮 Multiplayer Toolkit

The Multiplayer Toolkit brings additional multiplayer-related utilities
into the same All-In-One environment.

The long-term goal of Sacred Zonai Realms is to reduce the amount of
jumping between unrelated tools when setting up, maintaining, or
troubleshooting a multiplayer environment.

---

# 🩺 Diagnostics & Error Reports

If something goes wrong, don't just say:

> "It doesn't work."

The Diagnostics & Error Reports section exists to collect useful
technical information that can help identify what actually happened.

When reporting a problem, please include:

- Sacred Zonai Realms version
- Game version
- What tab/tool you were using
- SAV → KTML or KTML → SAV
- Player save or main realm save
- Whether the realm was paused
- Whether Docker Desktop was running
- What you clicked immediately before the error
- The exact error message
- Whether the problem happens every time
- A diagnostic report when available
- A screenshot if it helps

### 🔐 CHECK YOUR REPORT BEFORE SHARING IT.

Never publicly post:

- Passwords
- Authentication tokens
- API keys
- Private keys
- Browser cookies
- Login credentials
- Personal/private information

Even when automatic sanitization is available, **look at the report
yourself before posting it.**

---

# 🚀 What's New in v7.26?

Version **7.26** focuses heavily on reliability behind the scenes.

A lot of this release was spent dealing with problems that can happen
during real-world use rather than simply adding more buttons.

## 🐳 Improved Docker Pause / Resume

Docker Desktop isn't always immediately ready when an application tries
to communicate with it.

v7.26 improves this process by allowing the application to wait and
retry rather than immediately treating a temporary Docker startup delay
as a permanent failure.

When resuming a paused Docker-backed realm, the application gives
Docker additional time to become available.

If Docker still isn't available, Sacred Zonai Realms does not pretend
that the resume succeeded.

The realm can remain paused so the existing workspace and save data are
preserved.

If Docker returns but the previous container disappeared, the
application can recreate the container against the existing workspace
and saved port.

---

# 🛠️ Java Double → Long Save Fix

v7.26 specifically targets an important server-side error:

    java.lang.ClassCastException:
    java.lang.Double cannot be cast to java.lang.Long

Some values that should have been represented as integers could reach
the Java server as floating-point numbers.

For example:

    123.0

may logically need to be represented as:

    123

v7.26 improves server-oriented numeric normalization for supported
integer sections and arrays.

True fractional values are not supposed to be blindly converted into
integers.

If something such as:

    1.25

appears where an integer is required, the converter can reject the
questionable data rather than silently destroying the fractional
information.

Float-oriented data remains floating-point data.

---

# 💾 Improved Main Save Validation

Main realm save handling has also received additional server-oriented
normalization and validation.

The save can be parsed and reserialized using the server-compatible
type handling before the resulting data is trusted.

The goal is to catch incompatible data **before** it becomes a live
server problem.

---

# 🛡️ Safer Save Replacement

v7.26 places additional emphasis on protecting save replacement
operations.

The workflow now includes safeguards such as:

- Backup before replacement
- Validation
- UID protection
- Server-compatible save preparation
- Atomic replacement
- Rollback protection

The philosophy is simple:

**It is better for the application to stop and report a problem than to
quietly put questionable data into a live realm.**

---

# 🖥️ Windows EXE Builder Improvements

The Windows build process also received attention in v7.26.

The current builder includes additional:

- Preflight checks
- Required-resource checks
- Python detection
- PyInstaller handling
- Hidden-import handling
- Resource bundling
- Build diagnostics
- Build logging

If an EXE build fails, check:

    BUILD_v7.26_PYINSTALLER.log

That log can provide much more useful information than simply saying
that the EXE didn't build.

---

# 🎨 Customization

The All-In-One Edition includes interface customization options.

Supported wallpaper formats include common formats such as:

- PNG
- JPG / JPEG
- WEBP
- BMP
- GIF

Sound customization includes options for:

- Tab-transition sounds
- Success sounds
- Warning sounds
- Failure sounds

The existing master-volume and mute controls remain part of the
interface.

---

# 🆘 Something Went Wrong — What Do I Do?

First:

## DO NOT DELETE YOUR BACKUP.

Then determine exactly what happened.

Try to answer:

1. What were you trying to do?
2. Which tab were you using?
3. Which file did you select?
4. Was it `.SAV` or `.KTML`?
5. Was it a player save or main realm save?
6. Was the realm paused?
7. What happened after you clicked the button?
8. What error appeared?
9. Can you reproduce the problem?
10. Did the original backup still work?

Then create a diagnostic report and report the issue with as much
useful information as possible.

---

# ⚠️ Compatibility

This project is specifically developed around:

## Tears of the Kingdom v1.2.1

Do not assume saves from a different game version will behave exactly
the same way.

Always verify compatibility and always keep your original save.

---

# ❤️ Credits

Sacred Zonai Realms would not exist in its current form without the
work, research, tools, and community contributions of other people.

## 👨‍💻 ThyHeroOfTime / Legendary Savage Gamer

Developer and maintainer of the Sacred Zonai Realms All-In-One
application and the work of bringing these different tools and
workflows together.

## 🌿 Kirbymimi

Credit and thanks to **Kirbymimi**, creator of the Tears of the Kingdom
multiplayer/server project and the community surrounding it.

## 🌐 Wesley Da Man

Credit and thanks to **Wesley Da Man** for Zonai Hosting/self-hosting
concepts and source work that helped make the broader hosting and save
workflow possible.

## 💾 Marc Robledo

Credit and thanks to **Marc Robledo** and the other save-format
researchers and contributors whose research and reference work has
helped the community understand Tears of the Kingdom save data.

## ❤️ The Community

And thank you to everybody who:

- Tests releases
- Reports bugs
- Provides logs
- Shares useful information
- Helps other users
- Keeps these projects alive

Community testing is extremely important. Some problems only appear on
specific PCs, server configurations, saves, or multiplayer setups.

---

# ⚖️ Disclaimer

Sacred Zonai Realms is an independent community-created project.

It is not an official Nintendo product and is not affiliated with,
endorsed by, sponsored by, or approved by Nintendo.

The Legend of Zelda, Tears of the Kingdom, Nintendo Switch, and related
names and properties belong to their respective rights holders.

This project is intended as a community utility for working with
personally owned save data and compatible community multiplayer/server
environments.

---

# 🚨 FINAL WARNING

## BACK UP YOUR SAVE.

Seriously.

Before converting:

**BACK IT UP.**

Before editing:

**BACK IT UP.**

Before replacing a server save:

**BACK IT UP.**

Before testing something you're unsure about:

**BACK IT UP.**

Keep one untouched original somewhere safe.

    ORIGINAL
       |
       |---- SAFE BACKUP
       |       DO NOT TOUCH
       |
       └---- WORKING COPY
               |
               ├── Convert
               ├── Edit
               ├── Upload
               └── Test

If the working copy breaks, you still have your original.

---

# 🌿 Sacred Zonai Realms

### Tears of the Kingdom v1.2.1 Save Converter — All-In-One

**Version 7.26**

Created and maintained by:

**ThyHeroOfTime / Legendary Savage Gamer**

Thank you for downloading, testing, reporting bugs, and helping make
Sacred Zonai Realms better.
