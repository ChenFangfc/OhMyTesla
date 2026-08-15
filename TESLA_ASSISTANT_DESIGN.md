# Tesla Assistant — UI/UX Design System

## 1. Product Vision

Tesla Assistant is not a generic SaaS dashboard and not a chatbot attached to vehicle data.

It should feel like a lightweight, intelligent **Tesla operating layer** for the web:

- The **vehicle is the visual center** of the product.
- AI is a **system-wide command layer**, not an isolated chat page.
- Information should appear only when it is useful.
- The interface should feel calm, premium, technical, and extremely deliberate.
- Avoid visual clutter, unnecessary cards, heavy borders, and admin-dashboard conventions.

The core design direction is:

> **Tesla × Linear × xAI**

Recommended weighting:

- **Tesla — 50%**
  - Automotive identity
  - Radical minimalism
  - Vehicle-first hierarchy
  - Strong imagery
  - Black / graphite surfaces
  - Restrained use of Tesla red

- **Linear — 35%**
  - Application structure
  - Precise spacing
  - Fast and clean interactions
  - Excellent information hierarchy
  - Minimal borders
  - Dense information only when necessary

- **xAI — 15%**
  - AI interaction layer
  - Futuristic monochrome aesthetic
  - Command-oriented experience
  - Minimal conversational interface

The UI should feel like:

> “What if Tesla designed a web-native intelligent vehicle control center?”

---

# 2. Core Design Principles

## 2.1 Vehicle First

The vehicle should be the primary visual object on the main screen.

Do not make the homepage look like:

- a financial dashboard
- an admin panel
- a generic analytics tool
- a collection of 12 equal-sized cards
- a chatbot with car data attached

Instead, the hierarchy should be:

1. Vehicle
2. Current state
3. Most important actions
4. Contextual insights
5. AI interaction
6. Detailed analytics

The user should understand the state of their Tesla within 2–3 seconds.

---

## 2.2 Radical Subtraction

Every visible element must justify its existence.

Prefer:

- fewer components
- larger spacing
- stronger hierarchy
- progressive disclosure
- contextual controls
- simple labels
- direct numerical information

Avoid:

- decorative gradients everywhere
- unnecessary section titles
- excessive borders
- nested cards
- redundant icons
- too many badges
- overly dense dashboards

If two elements communicate the same thing, remove one.

---

## 2.3 Calm Technology

The product should feel futuristic without looking like a gaming UI.

Avoid:

- cyberpunk neon
- glowing borders everywhere
- excessive blur
- RGB gradients
- dramatic 3D UI decorations
- sci-fi HUD overload

Instead use:

- precise typography
- subtle depth
- monochrome surfaces
- smooth motion
- minimal accents
- large vehicle imagery
- carefully chosen data visualizations

---

## 2.4 AI Should Feel Native

AI should not feel like a separate product inside the Tesla Assistant.

Do not create a main navigation item called only:

> Chat

Instead, AI should be available from anywhere through a global input such as:

> Ask your Tesla...

The assistant should understand the current context automatically.

Examples:

- “Why did my efficiency drop today?”
- “When is the cheapest time to charge tonight?”
- “Can I make it to Chicago without charging?”
- “How much did charging cost this month?”
- “Turn on climate at 7:30 tomorrow morning.”
- “Is anything unusual with my battery?”
- “How much range am I losing because of the cold?”

The assistant should return structured actions and insights, not just paragraphs.

---

# 3. Visual Personality

The interface should communicate:

- Premium
- Quiet
- Precise
- Intelligent
- Automotive
- Modern
- Fast
- Trustworthy

It should NOT communicate:

- Enterprise SaaS
- Developer dashboard
- Gaming interface
- Crypto dashboard
- Social application
- Generic AI wrapper

---

# 4. Color System

## 4.1 Core Dark Theme

Dark mode should be the default experience.

```css
:root {
  --bg-primary: #0F0F10;
  --bg-secondary: #151516;
  --bg-elevated: #1A1A1C;
  --bg-hover: #202023;

  --text-primary: #F5F5F5;
  --text-secondary: #A0A0A5;
  --text-tertiary: #6F6F74;

  --border-subtle: rgba(255,255,255,0.07);
  --border-medium: rgba(255,255,255,0.12);

  --tesla-red: #E82127;
  --tesla-blue: #3E6AE1;
  --charging-green: #30D158;
  --warning-orange: #FF9F0A;
  --critical-red: #FF453A;
}
```

---

## 4.2 Accent Usage

Tesla red should be extremely restrained.

Use Tesla red for:

- brand mark
- destructive / critical action
- active alert
- high-priority warning
- one selected high-emphasis state

Do NOT use Tesla red as the primary color for:

- every button
- every icon
- every chart
- every active tab
- every link

Most of the interface should remain:

> black + graphite + white + gray

Operational states may use semantic colors:

- Charging → green
- Navigation / informational → blue
- Warning → orange
- Critical → red

---

# 5. Typography

Typography should feel neutral, modern, and technical.

Preferred style:

- Sans-serif
- High legibility
- Tight but not cramped
- Strong numeric readability
- Minimal stylistic personality

Suggested font stack:

```css
font-family:
  Inter,
  ui-sans-serif,
  -apple-system,
  BlinkMacSystemFont,
  "SF Pro Display",
  "SF Pro Text",
  "Segoe UI",
  sans-serif;
```

## Type Scale

### Hero vehicle information

Battery percentage:

```text
72%
```

Size: 48–72px  
Weight: 500–600

Range:

```text
214 mi
```

Size: 18–24px  
Weight: 400–500  
Color: secondary

### Section title

16–20px  
Weight: 500–600

### Data number

22–32px  
Weight: 500

### Supporting text

13–15px  
Weight: 400  
Color: secondary

### Metadata

11–13px  
Color: tertiary

Avoid heavy bold typography except for key states and numbers.

---

# 6. Spacing

Use generous whitespace.

Base spacing system:

```text
4
8
12
16
24
32
48
64
96
```

Main page horizontal padding:

Desktop:
```text
48–72px
```

Tablet:
```text
24–40px
```

Mobile:
```text
16–20px
```

Do not fill every empty area.

Whitespace is part of the visual system.

---

# 7. Border Radius

Keep radii subtle.

Recommended:

```text
Small controls:       8px
Buttons:              8–10px
Cards:                12–16px
Large assistant box:  16px
Modal / sheet:        16–20px
```

Avoid overly rounded “bubble UI.”

---

# 8. Borders and Depth

Default to border-light UI.

Cards should usually be separated through:

- spacing
- background change
- typography
- subtle elevation

instead of strong outlines.

Recommended:

```css
border: 1px solid rgba(255,255,255,0.07);
```

Shadows should be almost invisible.

Avoid:

- thick shadows
- floating glass cards everywhere
- strong outlines
- visible gradients around every component

---

# 9. Main Application Structure

Do NOT use a permanent left sidebar for the primary experience.

Recommended desktop structure:

```text
┌────────────────────────────────────────────────────────────┐
│ Tesla Assistant                         Vehicle ▾   Profile │
├────────────────────────────────────────────────────────────┤
│                                                            │
│                                                            │
│                   VEHICLE HERO AREA                        │
│                                                            │
│                                                            │
├────────────────────────────────────────────────────────────┤
│   Overview   Energy   Trips   Charging   Service   Settings│
├────────────────────────────────────────────────────────────┤
│                                                            │
│                   CONTEXTUAL CONTENT                       │
│                                                            │
│                                                            │
│               [ Ask your Tesla... ]                        │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

Navigation should be horizontal and restrained.

Suggested sections:

- Overview
- Energy
- Trips
- Charging
- Service
- Settings

AI should remain globally accessible rather than being one isolated tab.

---

# 10. Home / Overview Screen

The homepage should answer five questions immediately:

1. Where is my car?
2. What state is it in?
3. How much battery/range do I have?
4. Is anything important happening?
5. What can I do right now?

Recommended structure:

```text
┌─────────────────────────────────────────────────────────────┐
│ TESLA ASSISTANT                               ● Connected   │
│                                                             │
│                                                             │
│                  [ VEHICLE RENDER ]                         │
│                                                             │
│                          72%                                │
│                        214 mi                               │
│                                                             │
│        LOCK        CLIMATE       CHARGE       SENTRY        │
│                                                             │
│─────────────────────────────────────────────────────────────│
│                                                             │
│ Good morning.                                               │
│ Your Model 3 is ready.                                      │
│                                                             │
│ Charging                                    80% by 6:40 AM │
│ Energy today                                      31.2 kWh │
│ Estimated charging cost                              $2.14 │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Ask anything about your Tesla...                    ↑   │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ Overview     Energy     Trips     Charging      Service     │
└─────────────────────────────────────────────────────────────┘
```

---

# 11. Vehicle Hero

The vehicle render is the core visual asset.

It should occupy meaningful screen space.

Possible states:

- parked
- charging
- driving
- climate active
- sentry active
- door open
- trunk open
- charging port open

The visual may subtly update according to state.

Examples:

Charging:

```text
                 MODEL 3

             [vehicle render]

                    72%
                  214 mi

        Charging · 7.4 kW
        Complete by 6:40 AM
```

Parked:

```text
             [vehicle render]

                    78%
                  231 mi

               Parked
          Home · 2 minutes ago
```

Avoid placing the car inside a small card.

The car itself should define the visual center.

---

# 12. Primary Vehicle Controls

Display only the most frequently used controls on the main screen.

Recommended:

- Lock / Unlock
- Climate
- Charge
- Sentry

Optional contextual controls:

- Frunk
- Trunk
- Vent
- Flash
- Honk

Secondary controls should live behind:

> More controls

Icons should be simple line icons.

Each control:

```text
    icon

   Label
```

Avoid large colorful control tiles.

---

# 13. Status Language

Status text should be human-readable.

Prefer:

> Charging · 7.4 kW

instead of:

> charging_state = active

Prefer:

> 80% by 6:40 AM

instead of:

> Estimated completion time: 06:40

Prefer:

> Home · 2 minutes ago

instead of:

> Last vehicle location updated 120 seconds ago

---

# 14. Cards

Cards should not dominate the interface.

Good uses:

- charging session
- upcoming service
- AI insight
- trip summary
- anomaly warning

Bad use:

Creating a card for every metric.

Bad:

```text
[ Battery ]
[ Range ]
[ Temperature ]
[ Tire Pressure ]
[ Cost ]
[ Efficiency ]
[ Mileage ]
[ Charging ]
```

Better:

```text
Energy today

31.2 kWh
238 Wh/mi

↓ 8% better than your 30-day average

[compact chart]
```

One section can communicate multiple related values.

---

# 15. Energy Page

Energy should feel analytical but still Tesla-like.

Recommended hierarchy:

```text
Energy

This month

418 kWh
$42.81

↓ 12% vs last month


ENERGY USE

Driving            312 kWh
Climate              61 kWh
Standby               27 kWh
Other                 18 kWh


EFFICIENCY

243 Wh/mi

[ line chart ]


AI INSIGHT

Efficiency improved 7% this week,
mostly due to warmer temperatures.

[ Ask why ]
```

Do not overload the page with multiple dashboards.

Use one major chart per viewport whenever possible.

---

# 16. Charging Page

Charging should prioritize action.

Primary information:

- state
- battery
- charge limit
- current power
- completion time
- current price
- estimated session cost

Example:

```text
Charging

72% ━━━━━━━━━━━━━━━━●━━━━ 80%

7.4 kW

Complete by
6:40 AM

Estimated cost
$2.14


Charge limit
80%


Best rate tonight

1:00 AM – 5:30 AM
4.1¢ / kWh

[ Schedule charging ]
```

If electricity pricing is available, charging optimization should be one of the strongest AI features.

---

# 17. Trips Page

Trips should answer:

- where did I go?
- how far?
- how efficient?
- how much did it cost?
- was anything unusual?

Example:

```text
Springfield → Chicago

135 mi
2 h 17 min

Energy
34.8 kWh

Efficiency
258 Wh/mi

Estimated cost
$4.21


Assistant insight

Highway speed above 75 mph increased
energy consumption by approximately 9%.
```

---

# 18. AI Assistant

## 18.1 Global Input

The assistant should be present throughout the product.

Desktop:

```text
┌───────────────────────────────────────────────┐
│ Ask anything about your Tesla...          ↑ │
└───────────────────────────────────────────────┘
```

Possible placeholder rotation:

- Ask about your Tesla...
- Check my charging...
- Analyze today's efficiency...
- Plan my next trip...
- Is anything unusual?
- Find the cheapest charging time...

Do not use an aggressive “chatbot” visual identity.

---

## 18.2 Command Palette Behavior

Keyboard shortcut:

```text
⌘ K
```

or:

```text
⌘ /
```

opens the Tesla Assistant command surface.

Example:

```text
Ask your Tesla

> preheat tomorrow at 7:30

────────────────────────────

Schedule Climate
Tomorrow · 7:30 AM

Cabin temperature
72°F

[ Confirm ]
```

This should feel closer to Linear / Raycast command interaction than ChatGPT conversation UI.

---

# 19. AI Response Design

Responses should be structured.

Bad:

```text
Based on my analysis, it appears that your vehicle consumed
more energy today because several factors contributed...
```

Good:

```text
Efficiency was 14% lower today.

Main contributors

Cold weather           +6.8%
Highway speed          +4.1%
Cabin heating          +2.2%

[ View energy analysis ]
```

Another example:

```text
Best charging window

1:00 AM – 5:30 AM

Estimated cost
$4.82

Save
$3.17

vs charging now

[ Schedule charging ]
```

Another:

```text
You can reach Chicago.

Current range
214 mi

Trip distance
136 mi

Arrival estimate
28%

Recommended
No charging stop required.

[ Start trip ]
```

AI should often finish with an executable action.

---

# 20. AI Response Components

The assistant may use reusable structured components:

## Insight

```text
Efficiency improved 8% this week.
```

## Comparison

```text
Today          241 Wh/mi
30-day avg     263 Wh/mi
```

## Breakdown

```text
Driving        72%
Climate        14%
Standby         8%
Other           6%
```

## Recommendation

```text
Charge after 1:00 AM
to save approximately $3.17.
```

## Action

```text
[ Schedule charging ]
```

## Warning

```text
Tire pressure appears lower than normal.

Rear left
37 PSI

Usual
42 PSI

[ View details ]
```

---

# 21. AI Conversation History

Do not make conversation history the central layout.

Recent questions may appear in a lightweight drawer:

```text
Recent

Why was efficiency lower today?
Today, 9:42 PM

Cheapest charging time tonight
Yesterday

Can I reach Chicago?
Aug 3
```

Conversation history is secondary to the vehicle itself.

---

# 22. Notifications and Alerts

Alerts should be rare and meaningful.

Priority system:

### Critical

Example:

> Vehicle security alarm triggered.

Use red.

### Warning

Example:

> Rear-left tire pressure is below normal.

Use orange.

### Information

Example:

> Charging completed.

Use neutral or blue/green.

Do not make routine information look dangerous.

---

# 23. Empty States

Empty states should stay minimalist.

Example:

```text
No charging sessions yet.

Charging history will appear here
after your next session.
```

Optional action:

```text
[ Learn about charging insights ]
```

No illustrations unless they serve a real purpose.

---

# 24. Loading States

Avoid large spinners.

Prefer:

- skeleton text
- subtle shimmer
- progressive data reveal

Vehicle data should appear in priority order:

1. connection state
2. battery
3. vehicle status
4. controls
5. secondary analytics

---

# 25. Motion

Motion should communicate state, not decorate.

Recommended durations:

```text
Hover            120–160ms
Button           140–180ms
Panel            180–240ms
Modal            220–280ms
Page transition  220–320ms
```

Use:

```css
cubic-bezier(0.2, 0.8, 0.2, 1)
```

Good animations:

- battery value updates
- charging progress
- opening control panel
- AI result expansion
- command palette appearance
- vehicle state change

Avoid:

- constant glowing
- bouncing icons
- large parallax
- excessive spring physics
- animated gradients

---

# 26. Charts

Charts should match the interface.

Rules:

- dark background
- minimal grid lines
- no heavy borders
- restrained labels
- maximum one or two accent colors
- prioritize direct labels over legends

Example:

```text
Energy Usage

kWh

40 ┤
30 ┤              ╭───
20 ┤       ╭──────╯
10 ┤  ╭────╯
 0 ┼────────────────────
     M  T  W  T  F  S  S
```

Chart interaction should reveal detail on hover without cluttering the default view.

---

# 27. Tables

Avoid conventional enterprise-style tables unless necessary.

When a table is required:

- no vertical borders
- extremely subtle horizontal separators
- right-align numerical values
- use compact typography
- allow row hover

Example:

```text
Charging sessions

DATE          ENERGY      COST       LOCATION

Aug 7         42.1 kWh    $3.18      Home
Aug 5         31.4 kWh    $8.92      Supercharger
Aug 2         38.7 kWh    $2.76      Home
```

---

# 28. Responsive Design

## Desktop

Primary experience.

- large vehicle render
- wide centered content
- horizontal navigation
- AI input anchored near lower content area

Recommended content width:

```text
1200–1440px
```

---

## Tablet

- reduce vehicle image slightly
- keep primary controls horizontal
- collapse secondary analytics
- maintain vehicle-first hierarchy

---

## Mobile

Mobile should resemble a premium native Tesla companion app.

Suggested layout:

```text
Model 3                  ●

       [ vehicle ]

            72%
          214 mi

 Lock   Climate   Charge   Sentry

────────────────────────────

Charging
80% by 6:40 AM

Energy today
31.2 kWh

────────────────────────────

Ask your Tesla...

────────────────────────────

Overview   Energy   Trips   More
```

Use bottom navigation on mobile.

Do not force desktop navigation onto small screens.

---

# 29. Navigation

Desktop:

```text
Overview
Energy
Trips
Charging
Service
```

Settings should be visually secondary.

Vehicle selection may live in the top right:

```text
Model 3 ▾
```

Profile:

```text
Avatar
```

Mobile:

```text
Overview
Energy
Trips
More
```

AI remains globally accessible.

---

# 30. Buttons

Primary button:

- light background on dark UI
- dark text
- minimal radius

Example:

```text
[ Schedule charging ]
```

Secondary:

```text
[ View details ]
```

Text / ghost:

```text
Learn more →
```

Destructive:

```text
[ Stop charging ]
```

Only destructive or high-priority controls should use red.

---

# 31. Icons

Use a consistent thin-line icon set.

Recommended style:

- 1.5–2px strokes
- rounded line caps
- geometric
- minimal detail

Examples:

- lock
- snowflake
- battery
- bolt
- shield
- location
- route
- wrench
- tire
- temperature

Avoid mixing multiple icon libraries.

---

# 32. Vehicle Connection State

Connection should remain visible but subtle.

Examples:

```text
● Connected
```

```text
● Updating...
```

```text
○ Asleep
```

```text
○ Offline
```

Do not continuously wake the vehicle purely for UI freshness.

If data is stale:

```text
Last updated 8 min ago
```

---

# 33. Search vs Assistant

Do not create both a generic search bar and a generic AI bar.

The assistant can handle navigation.

Examples:

```text
> charging history
```

Result:

```text
Go to Charging History
```

```text
> tire pressure
```

Result:

```text
Vehicle Health
Rear Left: 37 PSI
...
```

This reduces navigation complexity.

---

# 34. Tesla Assistant Home Intelligence

The homepage should optionally surface 1–3 personalized insights.

Never more than three.

Example:

```text
INSIGHT

Charging after 1:00 AM tonight
could save approximately $3.17.

[ Schedule ]
```

```text
INSIGHT

Your efficiency improved 8%
compared with last week.

[ See why ]
```

```text
SERVICE

Rear-left tire pressure is
5 PSI below your usual level.

[ View vehicle health ]
```

These should be contextual and actionable.

---

# 35. Smart Daily Summary

Optional feature:

```text
Today

32 mi driven
8.4 kWh used
$0.76 estimated energy cost

Efficiency
262 Wh/mi

Battery
78% → 62%
```

AI summary:

```text
Slightly higher consumption than usual,
mainly due to cabin heating.
```

---

# 36. Design of Vehicle Health

Vehicle Health should focus on exceptions.

Do not show every sensor equally.

Default:

```text
Vehicle Health

Everything looks good.
```

If abnormal:

```text
Vehicle Health

1 item needs attention

Rear-left tire
37 PSI

Expected range
41–43 PSI
```

This makes abnormal states much easier to notice.

---

# 37. Service Experience

Service should be simplified into:

```text
Service

No immediate service needed.

Next recommended
Tire rotation

Estimated in
1,200 mi
```

If the assistant detects an issue:

```text
Rear-left tire pressure has remained
below normal for 3 days.

Recommended:
Inspect tire pressure before long-distance travel.

[ Find service options ]
```

---

# 38. Authentication Screen

Authentication should be minimal.

```text
TESLA ASSISTANT

Connect your Tesla

Securely connect your account
to view vehicle information.

[ Continue with Tesla ]

Your credentials are never stored.
```

Do not place unnecessary marketing content around the login flow.

---

# 39. Privacy and Trust

Because vehicle information is sensitive, clearly communicate trust.

Examples:

```text
Last synced 2 min ago
```

```text
Tesla Assistant does not store
your Tesla password.
```

```text
Vehicle commands require confirmation.
```

Potentially dangerous commands should require explicit confirmation.

Example:

```text
Unlock vehicle?

Model 3
Home

[ Cancel ] [ Unlock ]
```

---

# 40. Confirmation Rules

Require confirmation for:

- unlock
- open trunk
- open frunk
- vent windows
- stop charging
- remote start
- high-impact schedule changes
- security changes

Low-risk commands may execute immediately:

- climate temperature adjustment
- charge limit changes within safe bounds
- view analytics
- create informational reminders

---

# 41. Microcopy Style

Language should be concise.

Prefer:

> Charging complete.

Instead of:

> Your vehicle has successfully completed the charging process.

Prefer:

> Ready by 6:40 AM.

Instead of:

> The estimated charging completion time is 6:40 AM.

Prefer:

> 12% less energy than last week.

Instead of:

> Your vehicle consumed approximately twelve percent less energy in comparison with the preceding week.

---

# 42. Voice and Tone

The assistant should sound:

- concise
- confident
- neutral
- technically informed
- helpful

It should not sound:

- overly conversational
- cute
- emotional
- verbose
- sales-oriented

Bad:

> Great news! 🎉 Your Tesla is doing amazingly today!

Good:

> Vehicle status is normal.

Bad:

> Hmm, it looks like your efficiency took a bit of a hit today.

Good:

> Efficiency was 14% lower today.

---

# 43. Suggested Page Architecture

```text
/
├── overview
├── energy
│   ├── usage
│   ├── efficiency
│   └── cost
├── trips
│   └── [trip-id]
├── charging
│   ├── current
│   ├── history
│   └── schedule
├── vehicle
│   ├── controls
│   └── health
├── service
├── assistant
│   └── history
└── settings
    ├── vehicle
    ├── charging
    ├── notifications
    ├── privacy
    └── account
```

The `assistant` route can exist for history, but it should not be the only place where AI is accessible.

---

# 44. Component Architecture

Suggested reusable components:

```text
<AppShell />

<TopNavigation />
<MobileNavigation />

<VehicleHero />
<VehicleRender />
<VehicleStatus />
<BatteryStatus />

<QuickControls />
<VehicleControl />

<InsightCard />
<AlertCard />
<EnergySummary />
<ChargingSummary />
<TripSummary />
<HealthSummary />

<AssistantBar />
<AssistantCommandPalette />
<AssistantResponse />
<AssistantInsight />
<AssistantAction />

<Metric />
<MiniChart />
<EnergyChart />

<ConfirmationDialog />
<BottomSheet />
<Toast />
```

---

# 45. Desktop Homepage Reference Structure

```text
<AppShell>

  <TopNavigation />

  <main>

    <VehicleHero>

      <VehicleRender />

      <BatteryStatus
        battery="72%"
        range="214 mi"
      />

      <VehicleStatus>
        Parked · Home
      </VehicleStatus>

      <QuickControls>
        Lock
        Climate
        Charge
        Sentry
      </QuickControls>

    </VehicleHero>

    <OverviewSummary>

      <ChargingSummary />

      <EnergySummary />

      <HealthSummary />

    </OverviewSummary>

    <AssistantBar />

  </main>

</AppShell>
```

---

# 46. Homepage Density Rule

At initial load, try to limit the user to:

- 1 vehicle hero
- 4 primary controls
- 2–3 important summaries
- 1 AI input
- 1 navigation system

If significantly more content is visible, reconsider the information architecture.

---

# 47. Progressive Disclosure

Advanced information should appear on demand.

Example:

Initial:

```text
Energy today
31.2 kWh
```

Click:

```text
Energy today
31.2 kWh

Driving     22.4
Climate      4.2
Standby      2.8
Other        1.8

[ full chart ]
```

The default experience should never expose every available data point.

---

# 48. Visual Hierarchy Example

Correct:

```text
72%
214 mi

Charging · 7.4 kW
Complete by 6:40 AM
```

Incorrect:

```text
Battery State of Charge: 72%
Estimated EPA Range: 214 miles
Current Charging Power: 7.4 kilowatts
Estimated Charging Completion: 6:40 AM
```

Numbers should dominate.
Labels should support them.

---

# 49. Tesla-Inspired Brand Treatment

Avoid directly copying Tesla's website.

Instead borrow:

- restraint
- black / white visual language
- strong vehicle photography / renders
- geometric simplicity
- minimal controls
- premium typography
- cinematic spacing

The goal is not:

> “Clone Tesla.com.”

The goal is:

> “Create an interface that feels believable inside the Tesla ecosystem.”

---

# 50. Linear-Inspired Application Behavior

Borrow from Linear:

- excellent keyboard navigation
- fast interactions
- reduced chrome
- minimal borders
- small but precise controls
- command palette
- structured information hierarchy
- low visual noise

Do not borrow Linear's purple branding.

The interface should remain automotive.

---

# 51. xAI-Inspired Assistant Behavior

Borrow from xAI:

- monochrome AI surfaces
- strong typography
- minimal visual decoration
- direct response style
- futuristic tone

Do not turn the entire application into a chat product.

---

# 52. What NOT to Build

## Do not build a generic SaaS sidebar

Avoid:

```text
Dashboard
Analytics
Reports
Users
Settings
Billing
```

---

## Do not build a card grid

Avoid:

```text
┌─────────┐ ┌─────────┐ ┌─────────┐
│ Battery │ │ Range   │ │ Climate │
└─────────┘ └─────────┘ └─────────┘
┌─────────┐ ┌─────────┐ ┌─────────┐
│ Energy  │ │ Trips   │ │ Cost    │
└─────────┘ └─────────┘ └─────────┘
```

---

## Do not overuse glassmorphism

One slightly translucent overlay may work.

An entire glass UI will quickly feel generic.

---

## Do not use neon cyberpunk styling

This is a premium vehicle assistant, not a sci-fi game.

---

## Do not overuse gradients

Prefer flat graphite surfaces.

---

## Do not make AI a giant chat transcript

The user should interact with their vehicle, not stare at a chat log.

---

# 53. Signature Interaction

One defining interaction should be:

> **Ask your Tesla**

The user presses:

```text
⌘ K
```

and asks:

```text
Why did I use so much battery today?
```

The system responds immediately:

```text
Energy use was 18% above your normal weekday average.

Main contributors

Highway driving        +9%
Cabin heating          +6%
Sentry Mode            +3%

Your vehicle otherwise appears normal.

[ View energy timeline ]
```

This interaction represents the whole product philosophy:

- data
- intelligence
- explanation
- action
- minimal UI

---

# 54. Suggested Homepage Final Composition

```text
┌──────────────────────────────────────────────────────────────────┐
│ TESLA ASSISTANT                           Model 3 ▾   ● Connected │
│                                                                  │
│                                                                  │
│                         [ VEHICLE ]                              │
│                                                                  │
│                             72%                                  │
│                           214 mi                                 │
│                                                                  │
│                     Parked · Home                               │
│                                                                  │
│       ◉ Lock        ❄ Climate      ⚡ Charge       ◇ Sentry      │
│                                                                  │
│                                                                  │
│ ──────────────────────────────────────────────────────────────── │
│                                                                  │
│ Good morning.                                                    │
│ Your Model 3 is ready.                                           │
│                                                                  │
│ Charging                                              80% 6:40AM │
│ Energy today                                           31.2 kWh │
│ Vehicle health                                           Normal │
│                                                                  │
│                                                                  │
│ ┌──────────────────────────────────────────────────────────────┐ │
│ │ Ask anything about your Tesla...                         ↑  │ │
│ └──────────────────────────────────────────────────────────────┘ │
│                                                                  │
│       Overview      Energy      Trips      Charging      Service │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

# 55. Final Design Summary

The Tesla Assistant UI should follow one central idea:

> **The car is the interface. AI is the invisible intelligence behind it.**

Tesla contributes:

- vehicle-centric identity
- premium minimalism
- visual restraint

Linear contributes:

- interaction structure
- information hierarchy
- keyboard-first efficiency
- clean application architecture

xAI contributes:

- assistant tone
- command interaction
- futuristic minimalism

The final result should feel neither like a copied Tesla website nor like a generic AI dashboard.

It should feel like:

> **a believable next-generation Tesla web companion.**

---

# 56. Instructions for Coding Agents

When generating or modifying UI for this project, follow these rules:

1. Use a Tesla-inspired automotive visual language.
2. Use Linear-style application structure and interaction precision.
3. Use xAI-style minimalism for assistant experiences.
4. Keep the vehicle as the dominant visual object on the overview screen.
5. Do not introduce a permanent left sidebar unless a future information architecture absolutely requires one.
6. Prefer horizontal navigation on desktop and bottom navigation on mobile.
7. Use dark graphite surfaces instead of pure black everywhere.
8. Use borders only when necessary.
9. Use Tesla red sparingly.
10. Do not build dense metric-card grids.
11. Group related vehicle information into cohesive sections.
12. Use large numerical values and small supporting labels.
13. Keep AI accessible globally through “Ask your Tesla”.
14. AI responses should be structured, concise, and actionable.
15. Prefer command-style AI interactions over long conversational transcripts.
16. Every major AI answer should consider whether an action can follow.
17. Use progressive disclosure for advanced analytics.
18. Prioritize exceptions and anomalies instead of exposing every sensor equally.
19. Use one major chart per view whenever possible.
20. Avoid cyberpunk, neon, excessive gradients, and gaming aesthetics.
21. Keep animations subtle and functional.
22. Design mobile independently rather than merely shrinking desktop.
23. Use confirmations for safety-sensitive vehicle commands.
24. Use calm, direct microcopy.
25. If the page begins to resemble a generic SaaS dashboard, simplify it.

---

# 57. One-Sentence Prompt for Future UI Generation

Use this when instructing an AI coding agent:

> Design this interface as a Tesla-inspired, vehicle-first intelligent web application using Linear's precision and information hierarchy, Tesla's radical minimalism and automotive identity, and xAI's monochrome command-oriented AI experience; avoid generic SaaS dashboards, card grids, permanent sidebars, neon cyberpunk styling, and isolated chatbot interfaces.
