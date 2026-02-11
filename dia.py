import toupcam
import ctypes
import numpy as np
import time

def main():
    print('=== ToupCam Diagnostic Tool ===\n')
    
    a = toupcam.Toupcam.EnumV2()
    if len(a) > 0:
        print(f'Camera found: {a[0].displayname}')
        print(f'Model: {a[0].model}')
        print(f'ID: {a[0].id}\n')
        
        cam = toupcam.Toupcam.Open(a[0].id)
        if cam:
            try:
                # Resolution information
                print('=== Resolution Information ===')
                cam.put_eSize(0)
                width, height = cam.get_Size()
                print(f'Current resolution: {width} x {height}')
                
                # Check all available resolutions
                print('\nAvailable resolutions:')
                for i in range(3):
                    try:
                        cam.put_eSize(i)
                        w, h = cam.get_Size()
                        print(f'  Index {i}: {w} x {h}')
                    except:
                        break
                
                # Reset to highest resolution
                cam.put_eSize(0)
                width, height = cam.get_Size()
                
                # Exposure information
                print('\n=== Exposure Information ===')
                try:
                    expo_min, expo_max, expo_default = cam.get_ExpTimeRange()
                    print(f'Exposure time range: {expo_min} - {expo_max} us')
                    print(f'Default exposure: {expo_default} us')
                except Exception as e:
                    print(f'Could not get exposure range: {e}')
                
                try:
                    current_expo = cam.get_ExpoTime()
                    print(f'Current exposure: {current_expo} us ({current_expo/1000:.1f} ms)')
                except Exception as e:
                    print(f'Could not get current exposure: {e}')
                
                try:
                    auto_expo_enabled = cam.get_AutoExpoEnable()
                    print(f'Auto-exposure enabled: {auto_expo_enabled}')
                except Exception as e:
                    print(f'Could not check auto-exposure: {e}')
                
                # Gain information
                print('\n=== Gain Information ===')
                try:
                    gain = cam.get_ExpoAGain()
                    print(f'Current gain: {gain}')
                    if gain == 100:
                        print('  ⚠️  Gain is at maximum (100) - might cause noise')
                except Exception as e:
                    print(f'Could not get gain: {e}')
                
                # White balance information
                print('\n=== White Balance Information ===')
                try:
                    temp, tint = cam.get_TempTint()
                    print(f'Temperature: {temp}')
                    print(f'Tint: {tint}')
                except Exception as e:
                    print(f'Could not get white balance: {e}')
                
                # Try to capture a test image with current settings
                print('\n=== Test Capture ===')
                print('Starting camera...')
                cam.StartPullModeWithCallback(None, None)
                time.sleep(2)
                
                width, height = cam.get_Size()
                bits = 24
                bufsize = ((width * bits + 31) // 32 * 4) * height
                
                # CRITICAL FIX: Create ctypes buffer
                buf = (ctypes.c_ubyte * bufsize)()
                buf_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
                rowPitch = ((width * bits + 31) // 32) * 4

                
                print('Attempting capture...')
                try:
                    cam.PullImageV3(buf_ptr, 0, bits, rowPitch, None)
                    
                    # Calculate basic statistics
                    img = np.ctypeslib.as_array(buf)
                    
                    print(f'✓ Capture successful!')
                    print(f'  Buffer size: {bufsize:,} bytes')
                    print(f'  Min pixel value: {img.min()}')
                    print(f'  Max pixel value: {img.max()}')
                    print(f'  Mean pixel value: {img.mean():.2f}')
                    
                    if img.max() == 0:
                        print('\n⚠️  WARNING: All pixels are black (0)!')
                        print('   The camera is not receiving light or exposure is too low.')
                    elif img.mean() < 10:
                        print('\n⚠️  WARNING: Image is very dark (mean < 10)')
                        print('   Try increasing exposure time or check lighting.')
                    elif img.mean() > 245:
                        print('\n⚠️  WARNING: Image is overexposed (mean > 245)')
                        print('   Try decreasing exposure time or gain.')
                    else:
                        print(f'\n✓ Image brightness looks good (mean = {img.mean():.2f})')
                    
                    # Check for pixel variety
                    unique_values = len(np.unique(img))
                    print(f'  Unique pixel values: {unique_values:,}')
                    if unique_values < 10:
                        print('  ⚠️  Very few unique values - image might be corrupted')
                    
                except toupcam.HRESULTException as ex:
                    print(f'✗ Capture failed with HRESULT error: {ex.hr}')
                except Exception as ex:
                    print(f'✗ Unexpected error: {type(ex).__name__}: {ex}')
                    import traceback
                    traceback.print_exc()
                
                # Recommendations
                print('\n=== Recommendations ===')
                current_gain = cam.get_ExpoAGain()
                current_expo = cam.get_ExpoTime()
                
                if current_gain == 100:
                    print('• Reduce gain from 100 to 30-50 to reduce noise')
                    print('  Code: cam.put_ExpoAGain(50)')
                
                if current_expo < 5000:
                    print(f'• Exposure time is low ({current_expo}us). Consider increasing.')
                    print('  Code: cam.put_ExpoTime(50000)  # 50ms')
                
                print('\nBasic capture template:')
                print('```python')
                print('import toupcam')
                print('import ctypes')
                print('import numpy as np')
                print('from PIL import Image')
                print('import time')
                print('')
                print('cam = toupcam.Toupcam.Open(camera_id)')
                print('cam.put_eSize(0)  # Set resolution')
                print('width, height = cam.get_Size()')
                print('cam.put_AutoExpoEnable(True)')
                print('cam.put_ExpoAGain(50)')
                print('cam.AwbOnce()')
                print('cam.StartPullModeWithCallback(None, None)')
                print('time.sleep(3)')
                print('')
                print('# Create ctypes buffer (NOT bytes or bytearray)')
                print('bufsize = ((width * 24 + 31) // 32 * 4) * height')
                print('buf = (ctypes.c_ubyte * bufsize)()')
                print('')
                print('cam.PullImageV3(buf, 0, 24, 0, None)')
                print('img = np.ctypeslib.as_array(buf).reshape((height, width, 3))')
                print('img = img[:, :, ::-1]  # BGR to RGB')
                print('Image.fromarray(img).save("output.jpg")')
                print('```')
                
            finally:
                cam.Close()
                print('\n=== Camera closed ===')
        else:
            print('Failed to open camera')
    else:
        print('No camera found')

if __name__ == '__main__':
    main()