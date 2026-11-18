# Chat tool-use eval: input cases (for review)

26 cases, written by Claude (no real user traffic yet). Counts by kind: search 14, details 3, save 3, no-tools 4, no-data 2.

Expectations are what a correct *tool trajectory* looks like; the answer text is graded separately (see the grading proposal).

| id | kind | question | expected tool behaviour |
| --- | --- | --- | --- |
| s01 | search | Any good cafés within a short walk of Leith Walk? | search centred near **Leith Walk**; categories include any of cafe; radius ≤ 1500 m; must not call `save_place` |
| s02 | search | I'm at the Grassmarket and desperately need a toilet. | search centred near **Grassmarket**; categories include any of toilets; radius ≤ 1500 m; must not call `save_place` |
| s03 | search | Where can I refill my water bottle near the Royal Mile? | search centred near **Royal Mile**; categories include any of drinking_water; radius ≤ 1500 m; must not call `save_place` |
| s04 | search | Best viewpoints around Calton Hill? | search centred near **Calton Hill**; categories include any of viewpoint; radius ≤ 2000 m; must not call `save_place` |
| s05 | search | Somewhere for lunch near Haymarket station | search centred near **Haymarket**; categories include any of restaurant/cafe; radius ≤ 1500 m; must not call `save_place` |
| s06 | search | Parks near Marchmont for a picnic | search centred near **Marchmont**; categories include any of park; radius ≤ 2000 m; must not call `save_place` |
| s07 | search | Coffee in Stockbridge? | search centred near **Stockbridge**; categories include any of cafe; radius ≤ 1500 m; must not call `save_place` |
| s08 | search | Any restaurants near Dean Village? | search centred near **Dean Village**; categories include any of restaurant; radius ≤ 1500 m; must not call `save_place` |
| s09 | search | Toilets and a café near Princes Street please | search centred near **Princes Street**; categories include all of toilets + cafe; radius ≤ 1500 m; must not call `save_place` |
| s10 | search | We're walking up Arthur's Seat - anywhere to see the view or get water nearby? | search centred near **Arthur's Seat**; categories include any of viewpoint/drinking_water; radius ≤ 2000 m; must not call `save_place` |
| s11 | search | I'm in Bruntsfield with a toddler - nearest park? | search centred near **Bruntsfield**; categories include any of park; radius ≤ 1500 m; must not call `save_place` |
| s12 | search | cafes near the meadows | search centred near **The Meadows**; categories include any of cafe; radius ≤ 1500 m; must not call `save_place` |
| s13 | search | Where's good for dinner in the Grassmarket area? | search centred near **Grassmarket**; categories include any of restaurant; radius ≤ 1500 m; must not call `save_place` |
| s14 | search | Is there anywhere to eat within 200 metres of Calton Hill? | search centred near **Calton Hill**; categories include any of restaurant/cafe; radius ≤ 300 m; must not call `save_place` |
| d01 | details | What are Oh Deer's opening hours? It's the café on Leith Walk. | `get_place_details` on **Oh Deer**; must not call `save_place` |
| d02 | details | Is The Whispering Kettle on Leith Walk wheelchair accessible? | `get_place_details` on **The Whispering Kettle**; must not call `save_place` |
| d03 | details | When does Soderberg in Stockbridge close on Saturdays? | `get_place_details` on **Soderberg**; must not call `save_place` |
| v01 | save | Find me a café near Calton Hill and save it to my list. | search centred near **Calton Hill**; categories include any of cafe; radius ≤ 1500 m; must call `save_place` |
| v02 | save | Save Oh Deer on Leith Walk with the note 'brunch spot'. | `save_place` on **Oh Deer**, note contains “brunch” |
| v03 | save | What's a good café near the Meadows? I might go tomorrow. | search centred near **The Meadows**; categories include any of cafe; radius ≤ 1500 m; must not call `save_place` |
| n01 | no-tools | What makes a good picnic spot? | no tool calls at all |
| n02 | no-tools | How do I use this map? | no tool calls at all |
| n03 | no-tools | Thanks, that's all for now! | no tool calls at all |
| n04 | no-tools | Write a two-line poem about Edinburgh. | no tool calls at all |
| o01 | no-data | Any cafés in Glasgow city centre? | must call `find_location`; must not call `save_place` |
| o02 | no-data | Toilets near Edinburgh Airport? | must call `find_location`; must not call `save_place` |
