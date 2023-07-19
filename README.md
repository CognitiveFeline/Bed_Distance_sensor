##  The Future of 3D printer Bed Leveling

[BDsensor](https://www.pandapi3d.com/bdsensor) is the first distance sensor that can work with 3D printer and do fast bed leveling and adjust z axis in real time.

it can measure the bed distance at any point in real time without moving the z axis up and down.

 <img src="https://raw.githubusercontent.com/markniu/Bed_Distance_sensor/new/doc/images/Connection1.jpg" width="600" /> 

Faster leveling, realtime compensation, high accuracy.

1. No need to do probe points before every print, it will be automatically compensated for based on actual distance in real time.
 
2. You can do mesh bed leveling like a normal proximity sensor but much faster with this BDsensor.
   
3. Thanks for the distance value can be shown on the screen, you can do manual bed leveling more easily.

 [Test Video](https://www.youtube.com/watch?v=yx8pluEu0sg)
 . | BDsensor | BLtouch |superPINDA
--- | --- |--- |---
Sensor type |Distance Sensor| Proximity switch | Proximity switch
Distance resolution |0.005~0.01mm| None | None
Bed material |Metal| Unlimited | Metal
Communication |I2C [Timing Diagram](https://github.com/markniu/Bed_Distance_sensor/wiki/Data-Protocol)| PWM/Zendstop | Zendstop
Operating Range|4mm|None|None
Accuracy video: [BDsensor VS  Dial gauge](https://youtu.be/SLDsKLupcrk)


### Hardware:
Support any mainboard(8 bit or 32 bit) or Can bus module

Wire: Compatible with the BLtouch	connector. 


### Software
 . | Marlin | Klipper
--- | --- |---  
Real Time leveling |Support| No  
Fast probe |Support| Support 
Fast probe(No toolhead stop probe)|Support | Support 
Distance display |Support | Support 
Can bus toolhead|No | Support 



### Installation: [Marlin](https://github.com/markniu/Bed_Distance_sensor/wiki/Installing-Bed-Distance-Sensor) , [Klipper](https://github.com/markniu/Bed_Distance_sensor/wiki/Installing-for-Klipper) 



Project status: https://hackaday.io/project/185096-0006mm-distance-resolution-sensor-for-3d-printer

**Where to buy**: [https://www.pandapi3d.com/bdsensor](https://www.pandapi3d.com/bdsensor) , if you are in China please purchase it here
 [中国大陆淘宝店](https://item.taobao.com/item.htm?spm=a1z10.1-c.w4004-14344044600.5.60a16ff77NRBL5&id=684572042388)


<img alt="alt_text"   src="https://github.com/markniu/Bed_Distance_sensor/blob/new/doc/images/map.jpg" />
