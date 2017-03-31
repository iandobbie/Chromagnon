__author__ = "Atsushi Matsuda"



from . import cutoutAlign, alignfuncs, chromformat, aligner, chromeditor, threads, flatfielder, main

reload(cutoutAlign)
reload(alignfuncs)
reload(chromformat)
reload(aligner)
reload(chromeditor)
reload(threads)
reload(flatfielder)
reload(main)
try:
    from . import testfuncs
    reload(testfuncs)
except ImportError:
    pass

__version__ = main.__version__


#######
# Version history
#
# v0.5
# 1. Warning message for init guess was changed slightly
# 2. Included Bioformats (affected to aligner.py, ndviewer, listbox.py)
# 3. Added chromformat.py to accomodate string format for wavelength
# 4. Compatible with OpenCV3.3
# 5. Fixed instability when opening viewer on Linux
# 6. Fixed "nan" pixels in uint8 image after local alignment
# 0.53 Fixed compatibility of old chromagnon file and 2D images.
# 0.54 i) compatibility to arbitary file names for mrc.
# 0.54 ii) Fixed a wrong code in chromagnon reader. 20170327
# 0.54 iii) Fixed ziess's float wavelength (generalIO.getWaveIdx and chromformat)
# 0.54 iv) Fixed reftime in aligner.getshift()
# 0.54 v) FindRefwave added to time alignment (thread.py)
# 0.54 vi) time alignment is done only when nw==1 (changes in thread.py, aligner.py)
# 0.54 vii) setmaxerr pixsiz index was wrong.

# v0.4
# 1. Compatible with SIR images after discarding below 0.
# 2. Added a function to modify the output file suffix by the user.
# 3. Improved the criteria for the automatic Z mag calculation
# 4. Improved initial guess box
# 5. Added flat fielder
# 6. Fixed key event responses of the viewer
# 7. Fixed the crushing event when opening too many images
# 8. Added the "up" button for the file selector
# 9. The local distortion calculation was improved.
# 10.Added command line options.
# 11.Finding best reference sections are now compatible with bright field images.
# 12.Global correction includes 3DXcorr before finish.


# Things to do:
# * 3D local alignment
# * Add command line options for Flat fielder
