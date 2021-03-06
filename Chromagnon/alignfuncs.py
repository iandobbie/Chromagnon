from __future__ import print_function
import numpy as N
import sys

try:
    from Priithon.all import U, F
    from PriCommon import xcorr, imgResample, imgGeo,imgFilters

    if sys.version_info.major == 2 and hasattr(sys, 'app'):
        from PriCommon import ppro
    else:
        from PriCommon import ppro26 as ppro
        
except ImportError:
    from Chromagnon.Priithon.all import U, F
    from Chromagnon.PriCommon import xcorr, imgResample, imgGeo,imgFilters
    
    if sys.version_info.major == 2 and hasattr(sys, 'app'):
        from Chromagnon.PriCommon import ppro
    else:
        from Chromagnon.PriCommon import ppro26 as ppro
        
try:
    from . import cutoutAlign
except ValueError:
    from Chromagnon import cutoutAlign
except ImportError: # python2
    import cutoutAlign
from scipy import ndimage, spatial
#import exceptions


class AlignError(Exception): pass

CTHRE=0.065 * 0.3 # xcorr Gaussian profile was normalized to 0.3 to 1, then threshold also changed...
MAX_SHIFT = 5 #2 #0.4 # um
MIN_PXLS_YX = 60
MIN_PXLS_YXS = [str(30 * (2**i)) for i in range(4)]
MAX_SHIFT_LOCAL = 4#2 # pixel

QUADRATIC_AREA=['Right-Top', 'Left-Top', 'Left-Bottom', 'Right-Bottom']
IF_FAILED=['auto', 'force_logpolar', 'force_simplex', 'terminate']


def prep2D(a3d, zs=None):
    """
    simply makes maximum projection of best z sections

    return 2D-array
    """
    if zs is None:
        zs = findBestRefZs(a3d)

    aa = a3d[zs]
    return N.max(aa, axis=0)

def measureSaturation(a3d, force_calc_neighbor=False, ret_fraction=False):
    """
    return (number of saturated pixels, number of saturated pixels next to other saturated pixels)
    """
    try:
        ii = N.iinfo(a3d.dtype)
    except ValueError: # N.float
        return 0, 0
    
    if a3d.max() == ii.max:
        maxdist = N.linalg.norm(a3d.shape)
        # obtain indices of saturated pixels
        ind = N.array(N.where(a3d == ii.max)).T
        spx = len(ind)
        # examine if the coordinates are next to each other
        if spx / a3d.size < 0.0001 or force_calc_neighbor:
            dmat = spatial.distance.cdist(ind, ind, 'euclidean')
            # a mask for the diagnal 0
            eye = N.eye(dmat.shape[1], dmat.shape[0], dtype=dmat.dtype.type) * maxdist
            dmat += eye
            # obtain saturated pixels in clusters
            next_pxl_cubic = N.linalg.norm((2**0.5)+1)
            ind = N.where(dmat <= next_pxl_cubic)
            npxl = len(ind[0]) // 2 # removing redundancy
        else:
            npxl = spx

        if ret_fraction:
            return spx / a3d.size, npxl / (dmat.size // 2)
        else:
            return spx, npxl
    else:
        return 0, 0

def fixSaturation(a3d, sat=0, lowpass_sigma=0.1):
    """
    return lowpass filtered image if sat >0
    """
    if sat:
        a3d = lowPassGaussFilter(a3d, sigma=lowpass_sigma)
    return a3d

def lowPassGaussFilter(arr, sigma=0.5):
    """
    return lowpass filtered image even if arr.shape is odd.
    """
    shape = N.array(arr.shape)
    oddAxes = N.floor(shape % 2).astype(N.uint)
    if N.any(oddAxes):
        newshape = (shape + oddAxes).astype(N.uint)
        canvas = N.empty(newshape, arr.dtype.type)
        canvas[:] = N.median(arr)
        slcs = [slice(0,np) for np in shape]
        canvas[slcs] = arr
        arr = N.ascontiguousarray(canvas)
    arr = F.lowPassGaussFilter(arr, sigma=sigma)

    if N.any(oddAxes):
        arr = arr[slcs]
    return arr

##### ---- quadrisection phase correlation

def chopImg(a2d, center=None):
    """
    1|0
    ---
    2|3

    return a tuple of quadratic images
    """
    if center is None:
        center = N.array(a2d.shape) // 2
    center = [int(c) for c in center]
    r1 = a2d[center[0]:,center[1]:]
    r2 = a2d[center[0]:,:center[1]]
    r3 = a2d[:center[0],:center[1]]
    r4 = a2d[:center[0],center[1]:]
    return r1, r2, r3, r4


def estimate2D(a2d, ref, center=None, phaseContrast=True, cqthre=CTHRE/10., max_shift_pxl=5):
    """
    return [ty,tx,r,my,mx], offset, check
    """
    if center is None:
        shape = N.array(a2d.shape)
        center = shape // 2

    # threshold
    threfact = 0.03
    variance = getVar(a2d, ref)
    threshold = variance * threfact

    # separate quadrisection
    a1234 = chopImg(ref, center)
    b1234 = chopImg(a2d, center)
    shape = N.array(a1234[0].shape)

    # pre-treatments
    # in some reason, putting phaseContrastFilter here, not inside xcorr, gave better accuracy.
    npad = 4
    a1234p = [xcorr.paddAndApo(a, npad) for a in a1234]
    b1234p = [xcorr.paddAndApo(b, npad) for b in b1234]

    if phaseContrast:
        a1234p = [xcorr.phaseContrastFilter(N.ascontiguousarray(a)) for a in a1234p]
        b1234p = [xcorr.phaseContrastFilter(N.ascontiguousarray(b)) for b in b1234p]

    center_of_mass="""
    # obtain cm  (very minor improvement...)
    abp = [a + b1234p[i] for i, a in enumerate(a1234p)]
    xcms = [U.nd.center_of_mass(N.max(ab, axis=0)) for ab in abp]
    xcm1 = shape[1] - xcms[1] # left side should be flipped
    xcm2 = shape[1] - xcms[2]
    xcm = N.mean([xcms[0], xcm1, xcm2, xcms[3]])
    
    ycms = [U.nd.center_of_mass(N.max(ab, axis=1)) for ab in abp]
    ycm2 = shape[0] - xcms[2] # bottom side should be flipped
    ycm3 = shape[0] - xcms[3]
    ycm = N.mean([ycms[0], ycms[1], ycm2, ycm3])
    
    cm2 = N.array((ycm, xcm)) * 2"""

    # quadrisection cross correlation
    ab = list(zip(a1234p, b1234p))
    try:
        yxcs = [xcorr.Xcorr(a, b, phaseContrast=False, searchRad=max_shift_pxl) for a, b in ab]
    except IndexError:
        return N.array((0,0,0,1,1), N.float32), [0,0], [(i, 0) for i in range(4)]
    except (ValueError, ZeroDivisionError):
        raise AlignError
    yxs = [yx for yx, c in yxcs]
    
    # quality check
    cqvs = [c.max() - c[c.shape[0]//4].std() for yx, c in yxcs]
    
    checks = [(idx, cq) for idx, cq in enumerate(cqvs) if cq < cqthre]
    #ab_vars = [(a.var(), b.var()) for a, b in ab]
    #checks += [(idx, threshold) for idx, ab_var in enumerate(ab_vars) if ab_var[0] < threshold or ab_var[1] < threshold]
    
    del yxcs, ab#, c 

    # translation
    tyx = getTranslation(yxs)

    # magnification
    myx = getMagnification(yxs, center)#cm2)

    # rotation
    theta, offset = getRotation(yxs, center)#cm2)

    return list(tyx) + [theta] + list(myx), offset, checks

def getTranslation(yxs):
    """
    cqs: cross-correlation quality
    
    return [ty,tx]
    """
    tyx1 = (yxs[0] + yxs[2]) / 2.
    tyx2 = (yxs[1] + yxs[3]) / 2.

    tyx = N.array((tyx1, tyx2)).mean(axis=0)

    return tyx

def getMagnification(yxs, center):
    """
    return [my,mx]
    """
    my1 = (yxs[0][0] - yxs[3][0]) / 2.
    my2 = (yxs[1][0] - yxs[2][0]) / 2.

    my = (my1 + my2) / 2.

    mx2 = (yxs[0][1] - yxs[1][1]) / 2.
    mx1 = (yxs[3][1] - yxs[2][1]) / 2.

    mx = (mx1 + mx2) / 2.

    myx = N.array((my, mx), N.float32)
    myx = (myx + center) / center

    #print my1, my2, mx1, mx2
    return myx


def getRotation(yxs, center):
    """
    return r, offset_rotation
    """
    # angle increases toward left-up, thus y is plus while x is minus
    asiny1 = (yxs[0][0] - yxs[1][0]) / 2.
    asiny2 = (yxs[3][0] - yxs[2][0]) / 2.

    asiny = (asiny1 + asiny2) / 2.

    asinx1 = (yxs[3][1] - yxs[0][1]) / 2.
    asinx2 = (yxs[2][1] - yxs[1][1]) / 2.

    asinx = -(asinx1 + asinx2) / 2.

    # the center of the quadratic image should be determined
    center = N.array(center)
    center2 = center/2.
    theta = getTheta(asiny, asinx, center2)
    theta = N.degrees(theta)

    # rotation center but this does not seem to be working
    offx = 0#getOffset(asiny1, asiny2, asinx, theta, center2)
    #print offx
    offset = [0,offx]

    return theta, offset


def getTheta(asiny, asinx, center2=(100,100)):
    cdeg = N.arctan2(*center2)
    ryx = center2 + (asiny, asinx)
    return N.arctan2(*ryx) - cdeg

def getOffset(asiny1, asiny2, asinx, theta, center2):
    """
    trying to obtain rotation center offset arithmetically
    but this always gives the answer 0
    """
    theta1 = getTheta(asiny1, asinx, center2)
    theta2 = getTheta(asiny1, asinx, center2)

    tan0 = N.tan(theta)
    tan1 = N.tan(theta1)
    tan2 = N.tan(theta2)

    cos1 = N.cos(theta1)
    cos2 = N.cos(theta2)

    a1 = center2[1] * cos1
    a2 = center2[1] * cos2

    dx = (a1 * a2 * tan2 - a1 * a2 * tan1) / (a1 * tan1 + a2 * tan2)
    return dx

def iteration(a2d, ref, maxErr=0.01, niter=10, phaseContrast=True, initguess=None, echofunc=None, max_shift_pxl=5, cqthre=CTHRE/10.*1/0.3, if_failed=IF_FAILED[0]):#'simplex'):
    """
    iteratively do quadratic cross correlation

    a2d: image to be aligned
    ref: reference image
    maxErr: iteration is terminated when the calculated shift become less than this value (in pixel)
    niter: maximum number of iteration
    max_shift_pxl: number of pixels for you to allow the images to shift
    if_failed: 'terminate' or else

    return [ty,tx,r,my,mx] if_failed is 'terminate' and failed, return None
    """
    shape = N.array(a2d.shape)
    center = shape // 2

    ret = N.zeros((5,), N.float32)
    ret[3:] = 1
    if initguess is None or N.all(initguess[:2] == 0):
        yx, c = xcorr.Xcorr(ref, a2d, phaseContrast=phaseContrast)
        ret[:2] = yx
    else:
        #print('in iteration, initial geuss is', initguess)
        if echofunc:
            echofunc('in iteration, initial geuss is %s' % initguess, skip_notify=True)
        ret[:] = initguess[:]

    if if_failed == IF_FAILED[2]:#force_simplex':
        goodImg = 0#False
        niter = 1
    elif if_failed == IF_FAILED[1]:
        goodImg = -1
    else:
        goodImg = 1#True
    rough = True
                    
    offset = N.zeros((2,), N.float32)
    for i in range(niter):
        b = applyShift(a2d, [0]+list(ret), offset)

        # cut out
        # because irregular size of the quadratic images are not favorable,
        # the smallest window will be used
        shiftZYX = cutoutAlign.getShift([0]+list(ret), [0]+list(shape))
        maxcutY = max(shiftZYX[2], shape[0]-shiftZYX[3])
        maxcutX = max(shiftZYX[4], shape[1]-shiftZYX[5])
        slc = [slice(int(maxcutY), int(shape[0]-maxcutY)),
               slice(int(maxcutX), int(shape[1]-maxcutX))]
        b = b[slc]
        c = ref[slc]

        startYX = [maxcutY, maxcutX]

        if goodImg > 0:
            ll, curroff, checks = estimate2D(b, c, center-startYX+offset, phaseContrast=phaseContrast, max_shift_pxl=max_shift_pxl, cqthre=cqthre)

            if len(checks) <= 1:
                ret[:3] += ll[:3]
                ret[3:] *= ll[3:]

            # the quality of quadratic correlation affects resolution.
            # instead of using low-correlation results, such images are sent to alternative calculation.
            if len(checks):
                regions = [(QUADRATIC_AREA[idx], round(cq, 5)) for idx, cq in checks]
                msg = '%s quadratic regions had too-low correlation, using alternative algorithm' % regions
                if echofunc:
                    echofunc(msg)
                #print msg
                    
                if if_failed == 'terminate':
                    return ret, False
                elif if_failed == 'simplex':
                    goodImg = 0
                else:
                    goodImg = -1#False
                    checks = []
                    if initguess is not None:
                        ret[:] = initguess[:]
                #offset += curroff

        elif goodImg < 0:
            ll = logpolar(b, c, center-startYX+offset, phaseContrast)
            ret[:3] += ll[:3]
            ret[3:] *= ll[3:]
            
        elif goodImg == 0:
            #print('doing simplex')
            if echofunc:
                echofunc('doing simplex', skip_notify=True)
            ll = simplex(b, c, phaseContrast, rough=rough)
            ret[:3] += ll[:3]
            ret[3:] *= ll[3:]
            rough = False
            #if echofunc:
            #echofunc('%i: %s' % (i, ll))#ref))
        #print(i, ret)
        if echofunc:
            echofunc('%i %s' % (i, ret), skip_notify=True)
        errs = errPxl(ll, center)
        try:
            if len(maxErr) ==2:
                if N.all(errs[:2] < maxErr) and N.all(errs[2] < N.mean(maxErr)) and N.all(errs[3:] < maxErr):
                    break
        except TypeError: # maxErr is scaler
            if N.all(errs < maxErr):
                break


    return ret, True#, offset

def errPxl(yxrm, center):
    """
    align parameters are converted to pixel values

    yxrm: [ty,tx,r,my,mx]
    center: [cy,cx]

    return error value in pixel of [ty,tx,r,my,mx]
    """
    center = center / 2.
    radius = N.hypot(*center)

    # rotation
    r = yxrm[2]
    x = radius * N.cos(r) - radius
    y = radius * N.sin(r)
    rerr = N.hypot(x, y)
    
    # magnification
    myerr = (yxrm[3] * radius) - radius
    mxerr = (yxrm[4] * radius) - radius

    errlist = list(yxrm[:2]) + [rerr, myerr, mxerr]

    return N.abs(N.array(errlist))

def iterationXcor(a2d, ref, maxErr=0.01, niter=20, phaseContrast=True, initguess=None, echofunc=None):
    """
    find out translation along X axis
    this function is used for alignment of the Z axis
    parameters are the same as 'iteration'

    errorAxis: index if axis to examine error needs to be specified.

    return [ty,tx]
    """
    shape = N.array(a2d.shape)
    yxs = N.zeros((2,), N.float32)
    if initguess is not None:
        yxs[:] = initguess
        if echofunc:
            echofunc('initguess Xcorr: %s' % yxs, skip_notify=True)
    
    for i in range(niter):
        if i == 0 and initguess is None:
            b = a2d
            c = ref
            #startYX = [0,0]
        else:
            b = applyShift(a2d, [0]+list(yxs)+[0,1,1])#, offset)
            # cut out
            shiftZYX = cutoutAlign.getShift([0]+list(yxs)+[0,1,1], [0]+list(shape))
            maxcutY = max(shiftZYX[2], shape[0]-shiftZYX[3])
            maxcutX = max(shiftZYX[4], shape[1]-shiftZYX[5])
            slc = [slice(int(shiftZYX[2]), int(shiftZYX[3])),
                   slice(int(shiftZYX[4]), int(shiftZYX[5]))]
            b = b[slc]
            c = ref[slc]

        yx = xcorr.Xcorr(c, b, phaseContrast=phaseContrast)[0]
        yxs += yx
        #print('xcorr', i, yxs)
        if echofunc:
            echofunc('xcorr %i: %s' % (i, yxs), skip_notify=True)

        if N.all(N.abs(yx) < maxErr):
            break
        
    return yxs

###----- logpolar (alternative) method
def logpolar(a2d, ref, center=None, phaseContrast=True):
    shape = a2d.shape #N.array(a2d.shape, N.float32)
    if center is None:
        center = (shape[0] / 2., shape[1] / 2.)

    yx, c = list(xcorr.Xcorr(ref, a2d, phaseContrast=phaseContrast))
    #zyx = N.array([0] + yx)
    
   # arr = imgResample.trans3D_affine(a2d, zyx)
    
    a, log_base = imgResample.logpolar(a2d, center)
    b, log_base = imgResample.logpolar(ref, center)

    rm, c = xcorr.Xcorr(b, a, phaseContrast=phaseContrast)
    r = 180.0 * rm[0] / c.shape[0]
    m = log_base ** rm[1]
    
    #r = rm[0] / float(shape[0])
    #m = rm[1] / float(shape[1]) * mag

    yxrmm = list(yx) + [-r, m, m]

    return yxrmm
    


###----- simplex (alternative) method
def simplex(a, b, phaseContrast=True, rough=True):
    from scipy import optimize


    yxrm = N.zeros((5,), N.float64)
    #yxrm[:2] += yx
    yxrm[-2:] = 1

    # rough estimate
    if rough:
        yxrm[2] = roughRotMag(b, a, yxrm, 2, 0.02, 20)
        yxrm[3] = roughRotMag(b, a, yxrm, 3, 0.005, 20)
        yxrm[4] = roughRotMag(b, a, yxrm, 4, 0.005, 20)
    
    yxrm = optimize.fmin(_compCost, yxrm, (b, a), disp=0)

    # since xcorr is much better at yx estimation, yx part is replaced.
    #try:
    #    yx, c = xcorr.Xcorr(b, a, phaseContrast=phaseContrast)
    #    yxrm[:2] = yx
    #except (ValueError, ZeroDivisionError):
    #    raise AlignError

    return yxrm

def _compCost(yxrm, a, b):
    zyxrm = N.zeros((7,), N.float64)
    zyxrm[1:4] = yxrm[:3]
    zyxrm[5:] = yxrm[3:]
    
    b = applyShift(b, zyxrm)

    shape = a.shape
    shiftZYX = cutoutAlign.getShift([0]+list(yxrm), [0]+list(shape))
    maxcutY = max(shiftZYX[2], shape[0]-shiftZYX[3])
    maxcutX = max(shiftZYX[4], shape[1]-shiftZYX[5])
    slc = [slice(int(maxcutY), int(shape[0]-maxcutY)), # VisibleDeprecationWarning 20161216
           slice(int(maxcutX), int(shape[1]-maxcutX))]
    a = a[slc]
    b = b[slc]

    v = 1/ calcPearson(a, b)

    return v


def roughRotMag(a, b, yxrm, idx, step=0.02, nstep=20):
    guess = yxrm[idx]
    
    Range = (guess - (step*nstep), guess + (step * nstep), step)
    xs = N.arange(*Range)
    yxrms = N.empty((len(xs),)+yxrm.shape, yxrm.dtype.type)
    yxrms[:] = yxrm
    yxrms[:,idx] = xs
    #for r in Range:
    #    yxrm2 = N.copy(yxrm)
    #    yxrm2[idx] = r
    #    yxrms.append(yxrm2)

    pp = ppro.pmap(_compCost, yxrms, ppro.NCPU, a, b)
    #pp = [_compCost(yx, a, b) for yx in yxrms]
    xi = N.argmin(pp)
    pmin = pp[xi]

    base = max(pp)
    if pmin == base: # failed to get the curve for some reason
        # usually this happens if one used ndimage.zoom() in scipy
        # ndimage.zoom only interpolate pixel-wise.
        # no subpixel interpolation is done...
        x = int(idx >= 3)
        check = 5
    else: # go ahead and fit poly
        x = xs[xi]
        data = list(zip(xs,pp))
        fit, check = U.fitPoly(data, p=(1,1,1,1,1,1,1))

    if check == 5:
        answer = x
    else:
        xx = U.nd.zoom(xs, 100)
        yy = U.yPoly(fit, xx)
        ii = N.argmin(yy)
        answer = xx[ii]

    return answer

        

####


def findBestRefZs(ref, sigma=1):
    """
    PSF spread along the Z axis is often tilted in the Y or X axis.
    Thus simple maximum intensity projection may lead to the wrong answer to estimate rotation and magnification.
    On the other hands, taking a single section may also contain out of focus flare from neighboring sections that are tilted.
    Therefore, here sections with high complexity are selected and projected.

    ref: 3D array
    return z idx at the focus
    """
    nz = ref.shape[0]
    if ref.ndim == 2:
        return [0]
    elif nz <= 3:
        return list(range(nz))

    # Due to the roll up after FFT, the edge sections in Z may contain different information among the channels. Thus these sections are left unused.
    ms = N.zeros((nz -2,), N.float32)
    for z in range(1, nz-1):
        arr = ref[z]
        ms[z-1] = N.prod(U.mmms(arr)[-2:]) # mean * std

    mi,ma,me,st = U.mmms(ms)
    thr = me + st * sigma

    ids = [idx for idx in range(1,nz-1) if ms[idx-1] > thr]
    if not ids:
        ids = list(range(1,nz-1))

    return ids

def findBestRefZs(ref, sigma=0.5):
    """
    PSF spread along the Z axis is often tilted in the Y or X axis.
    Thus simple maximum intensity projection may lead to the wrong answer to estimate rotation and magnification.
    On the other hands, taking a single section may also contain out of focus flare from neighboring sections that are tilted.
    Therefore, here sections with high complexity are selected and projected.

    An intensity-based method does not work for most (eg. tissue or bright field) images.
    Using variance is not enough for bright field image where the right focus loses contrast.
    Thus here we used sections containing higher frequency.

    ref: 3D array
    return z idx at the focus
    """
    from Priithon.all import F
    nz = ref.shape[0]
    if ref.ndim == 2:
        return [0]
    elif nz <= 3:
        return list(range(nz))

    # ring array
    ring = F.ringArr(ref.shape[-2:], radius1=ref.shape[-1]//10, radius2=ref.shape[-2]//4, orig=(0,0), wrap=1)
    
    # Due to the roll up after FFT, the edge sections in Z may contain different information among the channels. Thus these sections are left unused.
    ms = N.zeros((nz -2,), N.float32)
    for z in range(1, nz-1):
        af = F.rfft(N.ascontiguousarray(ref[z]))
        ar = af * ring[:,:af.shape[-1]]
        ms[z-1] = N.sum(N.abs(ar))
        
        #ms[z-1] = N.prod(U.mmms(arr)[-2:]) # mean * std
    del ring, af, ar

    mi,ma,me,st = U.mmms(ms)
    thr = me + st * sigma

    ids = [idx for idx in range(1,nz-1) if ms[idx-1] > thr]
    if not ids:
        ids = list(range(1,nz-1))

    return ids


def applyShift(arr, zyxrm, dyx=(0,0)):
    """
    zyxrm: [tz,ty,tx,r,mz,my,mx]
    dyx: shift from the center of rotation & magnification

    return interpolated array
    """
    if N.any(zyxrm[:4]) or N.any(zyxrm[4:] != 1):
        zmagidx = 4

        if arr.ndim == 2:
            zyxrm = N.copy(zyxrm)
            zyxrm[0] = 0
            if len(zyxrm) == 7:
                zyxrm[-3] = 1
            zmagidx = -2

        arr = imgResample.trans3D_affine(arr, zyxrm[:3], zyxrm[3], zyxrm[zmagidx:], dyx)

    return arr

# ------ non linear  ------

def trans3D_affineVertical(img, affine):
    tzyx = (affine[0], 0, 0)
    r = 0
    mag = (affine[4], 1, 1)

    if tzyx[0] or (mag[0] != 1):
        return imgResample.trans3D_affine(img, tzyx=tzyx, r=r, mag=mag)
    else:
        return img

def remapWithAffine(img, mapzyx, affine, interp=2):
    """
    move alongZ then do remapping in 2D
    
    img: 3D image to be remapped
    mapzyx: index array with shape (z,2,y,x) or (2,y,x)
    affine: affine transform parameter as [tz,ty,yx,r,mz,my,mx]
    interp: interpolation order for remap (see imgResample.remap)

    return remapped image
    """
    if mapzyx.ndim == 4 and mapzyx.shape[0] == 1:
        mapzyx = mapzyx[0]
        #print 'mapzyx.ndim', mapzyx.ndim
    if mapzyx.ndim == 4:
        mapzyx = resizeLocal3D(mapzyx, img.shape[-3:])
    elif mapzyx.ndim == 3:
        mapzyx = resizeLocal2D(mapzyx, img.shape[-2:])

    if img.ndim > 2 and img.shape[-3] > 1:
        img = trans3D_affineVertical(img, affine)
    
    ret = N.indices(img.shape[-2:], N.float32)
    tyx = -affine[1:3] # minus to match with the cv coordinate
    r = affine[3]
    mag = affine[5:7]
    ret = imgGeo.affine_index(ret, r, mag, tyx)

    do = N.sum(affine[1:4]) + N.sum(affine[5:] - 1)

    arr2 = N.empty_like(img)
    for z, a in enumerate(img):
        if mapzyx.ndim == 4:
            mapy, mapx = mapzyx[z]
            do += mapzyx[z].sum()
        else:
            mapy, mapx = mapzyx

        if do or mapy.max() or mapx.max():
            arr2[z] = imgResample.remap(a, ret[0]+mapy, ret[1]+mapx)
        else:
            arr2[z] = a
    return arr2


# ------ non linear functions ------

def chopShapeND(shape, npxls=(32,32), shiftOrigin=(0,0)):#False):
    """
    shape: original image shape
    npxls: number of pixels to divide ((z,)y,x) or scaler
    shiftOrigin: start from half window right-up
    
    return list of [[[start, stop],...,], [[start, stop],...,]]
    """
    try:
        if len(npxls) != len(shape):
            raise ValueError('length of the list of npxls must be the same as len(shape)')
    except TypeError:
        npxls = [npxls for d in range(len(shape))]
        
    mimas = []
    for d, s in enumerate(shape):
        ndiv = s // npxls[d]
        mms = []
        start = (s - (npxls[d] * ndiv)) // 2
        half = npxls[d] // 2
        if start < half:
            start = half
        if not shiftOrigin[d]:
            start -= half
        for n in range(int(ndiv)):
            if (start + npxls[d]) >= s:
                break
            mms += [[start, start + npxls[d]]]
            start += npxls[d]
        mimas.append([slice(*m) for m in mms])
    return mimas

def chopImage2D(arr, npxls=(32,32), shiftOrigin=(0,0)):#False):
    """
    arr: image to be split
    npxls: number of pixels to divide (y,x) or scaler

    return (list_of_slice, list_of_arrs)
    """
    try:
        if len(npxls) != arr.ndim:
            raise ValueError('length of the list of npxls must be the same as arr.ndim')
    except TypeError:
        npxls = [npxls for d in range(arr.ndim)]
        
    chopSlcs = chopShapeND(arr.shape, npxls, shiftOrigin=shiftOrigin)

    #zslc = chopSlcs[0]
    yslc = chopSlcs[0]
    xslc = chopSlcs[1]

    arrs = []
    slcs = []
    #for zs in zslc:
    for ys in yslc:
        slc = [[ys,xs] for xs in xslc]
        arrs.append([arr[s] for s in slc])
        slcs.append(slc)
    return slcs, arrs

def chopImageND(arr, npxls=(32,32)):
    """
    arr: image to be split
    npxls: number of pixels to divide (y,x) or scaler

    return (list_of_slice, list_of_arrs)
    """
    try:
        if len(npxls) != arr.ndim:
            raise ValueError('length of the list of npxls must be the same as arr.ndim')
    except TypeError:
        npxls = [npxls for d in range(arr.ndim)]
        
    chopSlcs = chopShapeND(arr.shape, npxls)

    if len(chopSlcs) == 3:
        zslc = chopSlcs[-3]
    yslc = chopSlcs[-2]
    xslc = chopSlcs[-1]

    arrs = []
    slcs = []

    if len(chopSlcs) == 3:
        for zs in zslc:
            for ys in yslc:
                slc = [[zs,ys,xs] for xs in xslc]
                arrs.append([arr[s] for s in slc])
                slcs.append(slc)
    else:
        for ys in yslc:
            slc = [[ys,xs] for xs in xslc]
            arrs.append([arr[s] for s in slc])
            slcs.append(slc)
            
    return slcs, arrs

def calcPearson(a0, a1):
    a0div = a0 - N.average(a0)
    a1div = a1 - N.average(a1)

    r0 = N.sum(a0div * a1div)
    r1 = (N.sum(a0div**2) * N.sum(a1div**2)) ** 0.5
    return r0 / r1

### test funcs ---------------
def xcorNonLinearSingle(arr, ref, npxls=60, threshold=None, phaseContrast=True, cthre=CTHRE, pxlshift_allow=MAX_SHIFT_LOCAL, fillHole=True, niter=10):

    arrays = []
    regions = []
    yxs = []
    for i in range(niter):
        yx, region, c = xcorNonLinear(arr, ref, npxls, threshold, phaseContrast, cthre, pxlshift_allow)
        if fillHole:
            yx = fillHoles(yx, region[0])

        ret = paddYX(yx, npxls, arr.shape)#, maxcutY, maxcutX)

        #ret += N.indices(arr.shape, N.float32)
        ret2 = N.indices(arr.shape, N.float32) + ret
        arr2 = imgResample.remap(arr, ret2[0], ret2[1])

        tslcs, arrs = chopImage2D(arr, npxls)
        y0 = tslcs[0][0][0].start
        x0 = tslcs[0][0][1].start

        canvas = N.zeros_like(arr)
        for y in range(y0, arr.shape[0], npxls):
            canvas[y] = 1
        for x in range(x0, arr.shape[1], npxls):
            canvas[:,x] = 1

        arrays.append(N.array((arr2, ref, canvas, c, ret[0], ret[1])))
        regions.append(region)
        yxs.append(yx)
        
        arr = arr2
    return N.array(arrays), N.array(regions),N.array(yxs)

def getRegion(arr, ref, y, x, npxls=60, shiftOrigin=(0,0)):#False):
    slcs, arrs = chopImage2D(arr, npxls=(npxls, npxls), shiftOrigin=shiftOrigin)
    a = arrs[y][x]
    slcs, arrs = chopImage2D(ref, npxls=(npxls, npxls), shiftOrigin=shiftOrigin)
    b = arrs[y][x]
    return N.array((a, b))

def singleXcor(a, b):
    yx, c = xcorr.Xcorr(a, b)
    a2 = applyShift(a, [0]+list(-yx)+[0,1,1])
    return N.array((a2, b))

###---------------------

def xcorNonLinear_old(arr, ref, npxls=60, threshold=None, phaseContrast=True, cthre=CTHRE, pxlshift_allow=MAX_SHIFT_LOCAL):
    """
    arr: image to be registered
    ref: iamge to find the alignment parameter
    nplxs: number of pixels to divide (y,x) or scaler
    threshold: threshold value to decide if the region is to be cross correlated
    pahseContrast: phase contrast filter in cross correlation

    return (yx_arr, px_analyzed_arr[bool,var,cqual], result_arr)
    """
    if 0:#npxls < 60:
        phaseContrast = False 
        threfact = 0.2
        cfact = 3.
    else:
        threfact = 0.1#01#1
        cfact = 1
    
    try:
        if len(npxls) != len(arr.shape):
            raise ValueError('length of the list of npxls must be the same as len(shape)')
    except TypeError:
        npxls = [npxls for d in range(len(arr.shape))]
    
    tslcs, arrs = chopImage2D(arr, npxls)
    rslcs, refs = chopImage2D(ref, npxls)

    
    if threshold is None:
        #variance = (arr.var() + ref.var()) / 2.
        variance = getVar(arr, ref)
        threshold = variance * threfact

    nsplit = (len(tslcs), len(tslcs[0]))
    yxs = N.zeros((2,)+tuple(nsplit), N.float32)
    regions = N.zeros((3,)+tuple(nsplit), N.float32)
    cs = N.zeros(arr.shape, N.float32)
    for y, ay in enumerate(arrs):
        for x, a in enumerate(ay):
            b = refs[y][x]

            av = a#imgFilters.cutOutCenter(a, 0.5, interpolate=False)
            bv = b#imgFilters.cutOutCenter(b, 0.5, interpolate=False)
            #var = (N.var(av) + N.var(bv)) / 2. # crop to throw away the tip object
            var = getVar(av, bv)
            regions[1,y,x] = var
            if var > threshold:
                yx, c = xcorr.Xcorr(a, b, phaseContrast=phaseContrast)
                cs[tslcs[y][x]] = c
                csd = c[:c.shape[0]//4].std()
                cqual = (c.max() - csd) / (c.sum() / cfact)
                regions[2,y,x] = cqual
                if cqual >= cthre: # 0.3 is the max
                    if N.abs(yx).max() < pxlshift_allow:
                        yxs[:,y,x] = yx
                        regions[0,y,x] = 1
                del c

    del arrs, refs#, c
    return yxs, regions, cs

def xcorNonLinear(arr, ref, npxls=60, threshold=None, cthre=CTHRE, pxlshift_allow=MAX_SHIFT_LOCAL):
    """
    arr: image to be registered
    ref: iamge to find the alignment parameter
    nplxs: number of pixels to divide (y,x) or scaler
    threshold: threshold value to decide if the region is to be cross correlated
    pahseContrast: phase contrast filter in cross correlation

    return (yx_arr, px_analyzed_arr[bool,var,cqual], result_arr)
    """
    threfact = 0.1
    cfact = 1
    try:
        if len(npxls) != len(arr.shape):
            raise ValueError('length of the list of npxls must be the same as len(shape)')
    except TypeError:
        npxls = [npxls for d in range(len(arr.shape))]

    if threshold is None:
        variance = getVar(arr, ref)
        threshold = variance * threfact

    taas = [[chopImage2D(arr, npxls, shiftOrigin=(yi,xi)) for xi in range(2)] for yi in range(2)]
    tars = [[chopImage2D(ref, npxls, shiftOrigin=(yi,xi)) for xi in range(2)] for yi in range(2)]

    nsplit0 = N.array((len(taas[0][0][0]), len(taas[0][0][0][0])))
    nsplit1 = N.array((len(taas[1][1][0]), len(taas[1][1][0][0])))
    nsplit = tuple(nsplit0 + nsplit1)
    yxs = N.zeros((2,)+nsplit, N.float32)
    region = N.zeros((3,)+nsplit, N.float32)
    cs = N.zeros((2,2,) + arr.shape, N.float32)
        
    for yi in range(2):
        for xi in range(2):
            tslcs, arrs = taas[yi][xi]
            rslcs, refs = tars[yi][xi]

            for y, ay in enumerate(arrs):
                for x, a in enumerate(ay):
                    b = refs[y][x]

                    # crop to throw away the tip object
                    av = imgFilters.cutOutCenter(a, 0.5, interpolate=False)
                    bv = imgFilters.cutOutCenter(b, 0.5, interpolate=False)

                    var = getVar(av, bv)
                    region[1,2*y+yi,2*x+xi] = var
                    if var > threshold:
                        yx, c = xcorr.Xcorr(a, b)
                        #cs[[slice(i,i+1)]+tslcs[y][x]] += c
                        cs[[slice(yi,yi+1),slice(xi,xi+1)]+tslcs[y][x]] += c
                        csd = c[:c.shape[0]//4].std()
                        cqual = (c.max() - csd) / (c.sum() / cfact)
                        region[2,2*y+yi,2*x+xi] = cqual
                        if cqual >= cthre: # 0.3 is the max
                            if N.abs(yx).max() < pxlshift_allow:
                                yxs[:,2*y+yi,2*x+xi] = yx
                                region[0,2*y+yi,2*x+xi] = 1
                        del c


    del arrs, refs#, c
    return yxs, region, cs

def xcorNonLinear2(arr, ref, npxls=32, threshold=None, phaseContrast=True, cthre=CTHRE, pxlshift_allow=MAX_SHIFT_LOCAL):
    """
    arr: image to be registered
    ref: iamge to find the alignment parameter
    nplxs: number of pixels to divide (y,x) or scaler
    threshold: threshold value to decide if the region is to be cross correlated
    pahseContrast: phase contrast filter in cross correlation

    return (yx_arr, px_analyzed_arr[bool,var,cqual], result_arr)
    """
    if npxls < 60:
        phaseContrast = False 
        threfact = 0.2
        cfact = 3.
    else:
        threfact = 0.1#01#1
        cfact = 1
    
    try:
        if len(npxls) != len(arr.shape):
            raise ValueError('length of the list of npxls must be the same as len(shape)')
    except TypeError:
        npxls = [npxls for d in range(len(arr.shape))]
    
    tslcs, arrs = chopImage2D(arr, npxls)
    rslcs, refs = chopImage2D(ref, npxls)

    
    if threshold is None:
        #variance = (arr.var() + ref.var()) / 2.
        variance = getVar(arr, ref)
        threshold = variance * threfact

    nsplit = (len(tslcs), len(tslcs[0]))
    yxs = N.zeros((2,)+tuple(nsplit), N.float32)
    regions = N.zeros((3,)+tuple(nsplit), N.float32)
    cs = N.zeros(arr.shape, N.float32)
    for y, ay in enumerate(arrs):
        for x, a in enumerate(ay):
            b = refs[y][x]

            av = a#imgFilters.cutOutCenter(a, 0.5, interpolate=False)
            bv = b#imgFilters.cutOutCenter(b, 0.5, interpolate=False)
            #var = (N.var(av) + N.var(bv)) / 2. # crop to throw away the tip object
            var = getVar(av, bv)
            regions[1,y,x] = var
            yx0 = N.zeros((2,), N.float32)
            if var > threshold:
                for i in range(20):
                    yx, c = xcorr.Xcorr(a, b, phaseContrast=phaseContrast)
                    csd = c[:c.shape[0]//4].std()
                    cqual = (c.max() - csd) / (c.sum() / cfact)
                    if cqual >= cthre: # 0.3 is the max
                        yx0 += yx
                        if N.all(N.abs(yx) < 0.01):#maxErr):
                            #print 'break at %i' % i,
                            break
                        else:
                            a = applyShift(a, [0]+list(-yx0)+[0,1,1])
                            #return a, a0, yx0
                    else:
                        #print 'break c value y%i, x%i iter %i' % (y,x, i)
                        break
                if cqual >= cthre and N.abs(yx).max() < pxlshift_allow:
                    cs[tslcs[y][x]] = c
                    yxs[:,y,x] = yx0
                    regions[0,y,x] = 1
                    regions[2,y,x] = cqual
                    
                del c

    del arrs, refs#, c
    return yxs, regions, cs


def getVar(av, bv):
    #var =  (N.var(av)/N.mean(av) + N.var(bv)/N.mean(bv)) / 2.
    var =  (N.var(av) + N.var(bv)) / 2.

    return var

def cleanUpNonLinear(yxs, region):
    """
    fill the hold in the array (yxs) with median filtering
    the empty pixels were found using region

    return filled_yxs
    """
    medfilt = N.empty_like(yxs)

    for d, a in enumerate(yxs):
        medfilt[d] = ndimage.filters.median_filter(a, size=5)#3)

    return N.where(region, yxs, medfilt)

def findNonLinearSection(arr_ref_guess, npxl=MIN_PXLS_YX, affine=None, threshold=None, phaseContrast=True, maxErr=0.01):
    arr, ref, guess = arr_ref_guess
    return iterNonLinear(arr, ref, npxl=npxl, affine=affine, initGuess=guess, threshold=threshold, phaseContrast=phaseContrast, maxErr=maxErr)
        
def resizeLocal2D(arr, targetShape):
    """
    arr: mapzyx with shape (2,ny,nx)
    targetShape: (ny, nx)
    return resized 2D array
    """
    shape = N.array(arr.shape)
    tshape = N.array((2,)+tuple(targetShape))
    if N.all(shape == tshape):
        return arr
    elif N.all(shape >= tshape):
        return imgFilters.cutOutCenter(arr, tshape, interpolate=False)
    elif N.all(shape <= tshape):
        return imgFilters.paddingValue(arr, tshape, value=0, smooth=10, interpolate=False)
    else:
        raise NotImplementedError('The size of image does not match with the local distortion map')

def resizeLocal3D(arr, targetShape):
    """
    return resized 2D array
    """
    shape = N.array(arr.shape)
    targetShape = tuple(targetShape)
    tshape = N.array(targetShape[:1]+(2,)+targetShape[1:])
    if N.all(shape == tshape):
        return arr
    elif shape[0] < tshape[0]:
        canvas = N.zeros(tshape, arr.dtype.type)
        z0 = (tshape[0] - shape[0])//2
        for z, zt in enumerate(range(z0,shape[0]+z0)):
            canvas[zt] = resizeLocal2D(arr[z], targetShape[1:])
        return canvas
    elif shape[0] > tshape[0]:
        canvas = N.zeros(tshape, arr.dtype.type)
        z0 = (shape[0] - tshape[0])//2
        for zt, z in enumerate(range(z0,tshape[0]+z0)):
            canvas[zt] = resizeLocal2D(arr[z], targetShape[1:])
        return canvas

def paddYX(yx, npxl, shape, maxcutY=0, maxcutX=0):
    """
    """
    npxl //= 2
    
    #from Priithon.all import F
    pshape = (2,) + tuple([int(s) for s in N.array(yx.shape[-2:]) + 3*2])
    #yxp = F.getPadded(yx, pshape)
    #from Priithon.all import Y
    #yx = ndimage.zoom(yx, zoom=(1,npxl,npxl))#, order=1, prefilter=False)
    #yx = ndimage.zoom(yx, zoom=(1,npxl,npxl), order=1, prefilter=False) # bilinear is better
    #yx = ndimage.zoom(yx, zoom=(1,npxl,npxl), order=0, prefilter=False)
    yxp = imgFilters.zoomFourier(yx, (1,npxl,npxl))
    yx = yxp[:,3:-3,3:-3]
    shift="""
    yx = U.nd.shift(yx, (0,npxl/2,npxl/2))
    for z in xrange(npxl//2):
        yx[:,z] += yx[:,npxl//2+1]
        yx[:,:,z] += yx[:,:,npxl//2+1]"""
    
    # sometimes zoom has empty space at the edge
    spline="""
    if N.all(yx[:,:,-1]) == 0:
        yx[:,:,-1] = yx[:,:,-2]
    if N.all(yx[:,-1]) == 0:
        yx[:,-1] = yx[:,-2]"""

    mimas = chopShapeND(shape, npxls=(npxl,npxl), shiftOrigin=(1,1))#True)#False)
    #start = [mimas[0][0].start, mimas[1][0].start]
    #stop = [mimas[0][-1].stop, mimas[1][-1].stop]
    start = [mimas[0][0].start+npxl//2, mimas[1][0].start+npxl//2]
    stop = [mimas[0][-1].stop+npxl//2, mimas[1][-1].stop+npxl//2]
    
    #-- fill the mergin
    shape = N.array(shape)
    ndiv = shape // npxl
    #start = [int(i) for i in (shape - (npxl * ndiv)) // 2]
    #stop = [int(i) for i in start + (npxl * ndiv)] # VisibleDeprecationWarning
    shape = [int(i) for i in shape]
    start2 = N.array(start) + N.array((int(maxcutY), int(maxcutX)))
    start2 = [int(s) for s in start2]

    #print start, stop
    zeros = N.zeros((2,)+tuple(shape), N.float32)
    zeros[:,start2[0]:start2[0]+yx.shape[1],start2[1]:start2[1]+yx.shape[2]] += yx

    for d in range(2):
        # left
        for x in range(int(start2[1])):
            zeros[d,:start2[0],x] += yx[d,0,0]
            zeros[d,start2[0]:start2[0]+yx.shape[1],x] += yx[d,:,0]
            zeros[d,start2[0]+yx.shape[1]:,x] += yx[d,-1,0]
        # right
        for x in range(int(start2[1]+yx.shape[2]),int(zeros.shape[-1])):
            zeros[d,:start2[0],x] += yx[d,0,-1]
            zeros[d,start2[0]:start2[0]+yx.shape[1],x] += yx[d,:,-1]
            zeros[d,start2[0]+yx.shape[1]:,x] += yx[d,-1,-1]
        # bottom
        for y in range(int(start2[0])):
            zeros[d,y,:start2[1]] += yx[d,0,0]
            zeros[d,y,:start2[1]] /= 2.
            zeros[d,y,start2[1]:start2[1]+yx.shape[2]] += yx[d,0,:]
            zeros[d,y,start2[1]+yx.shape[2]:] += yx[d,0,-1]
            zeros[d,y,start2[1]+yx.shape[2]:] /= 2.
        # top
        for y in range(int(start2[0]+yx.shape[1]),int(zeros.shape[-2])):
            zeros[d,y,:start2[1]] += yx[d,-1,0]
            zeros[d,y,:start2[1]] /= 2.
            zeros[d,y,start2[1]:start2[1]+yx.shape[2]] += yx[d,-1,:]
            zeros[d,y,start2[1]+yx.shape[2]:] += yx[d,-1,-1]
            zeros[d,y,start2[1]+yx.shape[2]:] /= 2.

    return zeros

## ---- remove outside of significant signals -----

def iterNonLinear_old(arr, ref, npxl=MIN_PXLS_YX, affine=None, initGuess=None, threshold=None, phaseContrast=True, niter=10, maxErr=0.01, cthre=CTHRE, echofunc=None):
    """
    arr: image to be registered
    ref: image to find the alignment parameter
    nplx: number of pixels to divide, a scaler
    initGuess: initGuess of the nonlinear parameter
    affine: affine transform parameter as [tz,ty,yx,r,mz,my,mx]
    threshold: threshold value to decide if the region is to be cross correlated
    pahseContrast: phase contrast filter in cross correlation
    niter: maximum number of iteration
    maxErr: minimum error in pixel to terminate iterations

    return (yx_arr, px_analyzed_arr, resulting_arr)
    """
    rrs=[]
    yxs=[]
    #css=[]
    ars=[]
    
    shape = arr.shape
    last_shape = None
    
    #-- prepare output array
    ret = N.indices(arr.shape, N.float32) # hold affine + mapyx + initial_guess

    # to return something similar to yxc in case of no iteration was done.
    tslcs, arrs = chopImage2D(arr, npxl)
    nsplit = (len(tslcs), len(tslcs[0]))

    maxcutY = maxcutX = 0
    if affine is not None:
        tyx = -affine[1:3] # minus to match cv coordinate
        r = affine[3]
        mag = affine[5:7]
        ret = imgGeo.affine_index(ret, r, mag, tyx)

        # cut out
        shiftZYX = cutoutAlign.getShift(affine, [0]+list(shape))
        maxcutY = max(shiftZYX[2], shape[0]-shiftZYX[3])
        maxcutX = max(shiftZYX[4], shape[1]-shiftZYX[5])
        slc = [slice(int(maxcutY), int(shape[0]-maxcutY)),
               slice(int(maxcutX), int(shape[1]-maxcutX))]

    if initGuess is not None:
        ret += initGuess

    for i in range(niter):
        #-- first apply the initial guess
        if i or affine is not None or initGuess is not None:
            arr2 = imgResample.remap(arr, ret[0], ret[1])

            if affine is not None:
                arr2 = arr2[slc]
                ref2 = ref[slc]
            else:
                ref2 = ref
        else:
            arr2 = arr
            ref2 = ref

        #-- calculate local cross corelation
        if threshold is None:
            variance = getVar(arr2, ref2)#(arr2.var() + ref2.var()) / 2.
            if npxl < 60:
                threfact = 0.2
            else:
                threfact = 0.1
            threshold = variance * threfact#0.1
        yx, region, cs = xcorNonLinear(arr2, ref2, npxl, threshold, phaseContrast, cthre)
        yxs.append(yx)
        rrs.append(region)
        #css.append(cs)
        yx = fillHoles(yx, region[0])
        try:
            regions
        except NameError:
            regions = N.zeros(region[0].shape, N.uint16)
            yxc = N.zeros_like(yx)

        regions = N.add(regions, region[0]) # DEBUG: ufunc casting rule
        if not region[0].sum():
            if N.any(region[1] > threshold):
                if echofunc:
                    echofunc('variance high enough (%.1f > %.1f) but quality of CC too low (%.3f)' % (region[1].max(), threshold, region[2].max()))
            else:
                if echofunc:
                    echofunc('variance too low (%.1f < %.1f)' % (region[1].max(), threshold))
            break

        npxls = region[0].sum()
        err = N.sqrt(N.sum(N.power(yx[...,N.nonzero(region[0])], 2))) / float(npxls)
        rgn = N.nonzero(region[0].ravel())[0]
        ccq = region[2].ravel()[rgn]
        if echofunc:
            echofunc('(nplxs: %i) iteration %i: num_regions=%i, min_cc=%.4f, pxl_shift_mean=%.4f, max=%.4f' % (npxl, i, npxls, ccq.min(), err, N.sqrt(N.sum(N.power(yx,2), axis=-1)).max()))

        #-- smoothly zoom up the non-linear alignment parameter
        yxc += yx

        yx = paddYX(yx, npxl, arr.shape, maxcutY, maxcutX)
        ars.append(N.array((arr2,ref2,cs, yx[0],yx[1])))

        #-- combine result
        ret += yx

        if err < maxErr:
            break

    return yxc, regions, N.array((ref2, arr2, cs, yx[0], yx[1])), yxs, rrs, N.array(ars)#css, ars

def iterNonLinear(arr, ref, npxl=MIN_PXLS_YX, affine=None, initGuess=None, threshold=None, phaseContrast=True, niter=5, maxErr=0.01, cthre=CTHRE, echofunc=None, debug=False):
    """
    arr: image to be registered
    ref: image to find the alignment parameter
    nplx: number of pixels to divide, a scaler
    initGuess: initGuess of the nonlinear parameter
    affine: affine transform parameter as [tz,ty,yx,r,mz,my,mx]
    threshold: threshold value to decide if the region is to be cross correlated
    pahseContrast: phase contrast filter in cross correlation
    niter: maximum number of iteration
    maxErr: minimum error in pixel to terminate iterations

    return (yx_arr, px_analyzed_arr, resulting_arr)
    """
    rrs=[] # quality measure
    yxs=[] # list of shifts
    #css=[]
    ars=[] # arrays for debug
    
    shape = arr.shape
    last_shape = None
    
    #-- prepare output array
    ret = N.indices(arr.shape, N.float32) # hold affine + mapyx + initial_guess

    # to return something similar to yxc in case of no iteration was done.
    #tslcs, arrs = chopImage2D(arr, npxl)
    #nsplit = (len(tslcs), len(tslcs[0]))

    maxcutY = maxcutX = 0
    if affine is not None:
        tyx = -affine[1:3] # minus to match cv coordinate
        r = affine[3]
        mag = affine[5:7]
        ret = imgGeo.affine_index(ret, r, mag, tyx)

        # cut out
        shiftZYX = cutoutAlign.getShift(affine, [0]+list(shape))
        maxcutY = max(shiftZYX[2], shape[0]-shiftZYX[3])
        maxcutX = max(shiftZYX[4], shape[1]-shiftZYX[5])
        slc = [slice(int(maxcutY), int(shape[0]-maxcutY)),
               slice(int(maxcutX), int(shape[1]-maxcutX))]

    if initGuess is not None:
        ret += initGuess


    if debug:
        tslcs, arrs = chopImage2D(arr, npxl)
        del arrs
        y0 = tslcs[0][0][0].start
        x0 = tslcs[0][0][1].start

        canvas = N.zeros_like(arr)
        #for y in xrange(y0-npxl//4, arr.shape[0], npxl//2):
        for y in range(y0+npxl//2, arr.shape[0], npxl):
            canvas[y] = 1
        #for x in xrange(x0-npxl//4, arr.shape[1], npxl//2):
        for x in range(x0+npxl//2, arr.shape[1], npxl):
            canvas[:,x] = 1

    for i in range(niter):
        #-- first apply the initial guess
        if i or affine is not None or initGuess is not None:
            arr2 = imgResample.remap(arr, ret[0], ret[1])

            if affine is not None:
                arr2 = arr2[slc]
                ref2 = ref[slc]
            else:
                ref2 = ref
        else:
            arr2 = arr
            ref2 = ref

        #-- calculate local cross corelation
        if threshold is None:
            variance = getVar(arr2, ref2)#(arr2.var() + ref2.var()) / 2.
            #if npxl < 60:
            #    threfact = 0.2
            #else:
            threfact = 0.1
            threshold = variance * threfact#0.1
        yx, region, cs = xcorNonLinear(arr2, ref2, npxl, threshold, cthre)#phaseContrast, cthre)
        # some window sizes simply increases errors during iteration...
        # this is not easy to predict
        # for example, using arr.shape = (1008, 1012)
        # following window size did not work
        if i and debug:
            print(i, N.abs(yx).mean(), N.abs(yxs[i-1]).mean())
        if i and N.abs(yx).mean() > N.abs(yxs[i-1]).mean() and not debug:
            return None, N.zeros((1,)), None #None, None
        yxs.append(yx)
        if debug:
            rrs.append(region)

        
        yx = fillHoles(yx, region[0])
        try:
            regions
        except NameError:
            regions = N.zeros(region[0].shape, N.uint16)
            yxc = N.zeros_like(yx)

        regions = N.add(regions, region[0]) # DEBUG: ufunc casting rule
        if not region[0].sum():
            if N.any(region[1] > threshold):
                if echofunc:
                    echofunc('variance high enough (%.1f > %.1f) but quality of CC too low (%.3f)' % (region[1].max(), threshold, region[2].max()))
            else:
                if echofunc:
                    echofunc('variance too low (%.1f < %.1f)' % (region[1].max(), threshold))
            break

        npxls = region[0].sum()
        #err = N.sqrt(N.sum(N.power(yx[(Ellipsis,)+N.nonzero(region[0])], 2))) / float(npxls)
        errs = N.sqrt(N.sum(N.power(yx[(Ellipsis,)+N.nonzero(region[0])], 2), axis=-1))
        max_err = errs.max()
        mean_err = errs.mean()
        rgn = N.nonzero(region[0].ravel())[0]
        ccq = region[2].ravel()[rgn]
        if echofunc:
            echofunc('(nplxs: %i) iteration %i: num_regions=%i, min_cc=%.4f, pxl_shift_mean=%.4f, max=%.4f' % (npxl, i, npxls, ccq.min(), mean_err, max_err))#N.sqrt(N.sum(N.power(yx,2), axis=-1)).max()))

        #-- smoothly zoom up the non-linear alignment parameter
        yxc += yx

        yx = paddYX(yx, npxl, arr.shape, maxcutY, maxcutX)

        if debug:
            #ars.append(N.array((arr2,ref2,cs[1,1],N.mean((cs[0,0], cs[0,1],cs[1,0]), axis=0), yx[0],yx[1], canvas)))
            ars.append(N.array((arr2,ref2,cs[1,1],cs[0,0], cs[0,1],cs[1,0])))

        #-- combine result
        ret += yx

        if max_err < maxErr:#err < maxErr:
            break

    if debug:
        return yxc, regions, rrs, N.array(ars)
    else:
        #del arr2, ref2, cs
        return paddYX(yxc, npxl, arr.shape, maxcutY, maxcutX), regions, N.array((arr2, ref2, cs[0,0], cs[0,1], cs[1,0],cs[1,1]))

def makeWin(shape, minwin, maxwin=300):
    win0 = min((float(min(shape//2)), maxwin))
    series = int(N.log2(win0 / minwin)) + 1
    #print(series)
    wins0 = win0 // (2 ** N.arange(series))
    wins = []
    for win in wins0:
        win = int(win)
        if win % 2:
            win += 1
        wins.append(win)
    return wins

no_meaning="""
def makeWin(shape, minwin):
    shape = N.array(shape)
    minwin0 = minwin
    found = False
    for i in xrange(10):
        if N.any(shape % minwin0) == 0:
            minwin0 += 2
        else:
            found = True
            break
    if found:
        minwin = minwin0
        
    wins = [minwin * (2**i) for i in xrange(int(N.log2(min(shape)/minwin)))]
    wins = [w + w % 2 for w in wins]
    return wins[::-1]"""
    
    
    
def iterWindowNonLinear(arr, ref, minwin=MIN_PXLS_YX, affine=None, initGuess=None, threshold=None, phaseContrast=True, niter=5, maxErr=0.01, cthre=CTHRE, echofunc=None):
    """
    return (yx_arr, px_analyzed_arr, result_arr)
    """
    shape = N.array(arr.shape)

    maxcutY = maxcutX = 0
    if affine is not None:
        shiftZYX = cutoutAlign.getShift(affine, [0]+list(shape))
        maxcutY = max(shiftZYX[2], shape[0]-shiftZYX[3])
        maxcutX = max(shiftZYX[4], shape[1]-shiftZYX[5])
        shape = N.subtract(shape, (maxcutY*2, maxcutX*2))
    old="""
    win0 = float(min(shape//4))

    series = int(N.sqrt(win0 / minwin))# + 1
    print win0, series
    wins0 = win0 // (2 ** N.arange(series))
    wins = []
    for win in wins0:
        win = int(win)
        if win % 2:
            win -= 1
        wins.append(win)
    print wins"""
    wins = makeWin(shape, minwin)
    #print(wins)
    if not wins:
        return N.zeros((2,)+arr.shape, N.float32), None, None
        
    currentGuess = initGuess
    ## After v0.5.7, window size became difficult to predict at this point
    ## yx waas simply assembled into the image size instead of minimum size possible.
    old="""
    tmp_shape = [int(n) for n in (2,) + tuple(shape//wins[-1])] # VisibleDeprecationWarning
    yx_acc = N.zeros(tmp_shape, N.float32)"""
    #yxs = N.zeros((2,)+arr.shape, N.float32)
    
    for i, win in enumerate(wins):
        if echofunc:
            echofunc('--current window size: %i' % win)
        for ii in range(10):
            yxc, regions, arr2 = iterNonLinear(arr, ref, npxl=win, affine=affine, initGuess=currentGuess, threshold=threshold, phaseContrast=phaseContrast, niter=niter, maxErr=maxErr, cthre=cthre, echofunc=echofunc)
            if yxc is None:
                if echofunc:
                    echofunc('window size %i did not work!, change to %i (trial %i)' % (win, win+2, ii))
                #print 'ii=%i, window size %i did not work!, change to %i' % (i, win, win+2)
                win += 2
                yxc = 0
            else:
                break

        #return yxc, win
        #yxc, regions = iterNonLinear(arr, ref, npxl=win, affine=affine, initGuess=currentGuess, threshold=threshold, phaseContrast=phaseContrast, niter=niter, maxErr=maxErr, cthre=cthre, echofunc=echofunc)

        #print 'shape', yxc.shape, yx_acc.shape, arr.shape

        #yxz = paddYX2(yxc, 2**(series-1-i), yx_acc.shape[-2:])
        #yx_acc += yxz
        #yxs += yxc

        if currentGuess is None:
            #currentGuess = paddYX(yxc, win, arr.shape, maxcutY, maxcutX)
            currentGuess = yxc
        else:
            #currentGuess += paddYX(yxc, win, arr.shape, maxcutY, maxcutX)
            currentGuess += yxc

        rmax = regions.max()
        if not rmax:
            if echofunc:
                echofunc('  no region was found to be good enough')
            break
        else:
            #print('rmax: %.2f, -- continue' % rmax)
            if echofunc:
                echofunc('rmax: %.2f, -- continue' % rmax, skip_notify=True)

    old="""
    # throw away regions with low contrast
    if rmax:
        #thre = N.where(regions == rmax, 1, 0)
        #yx_acc[:] *= thre#N.where(regions == rmax, 1, 0)
        yxs = paddYX(yx_acc, wins[-1], arr.shape, maxcutY, maxcutX)
    else:
        yxs = N.zeros((2,)+arr.shape, N.float32)"""

    #yxs = currentGuess
    #if initGuess is not None:
    #    yxs += initGuess

    return currentGuess, regions, arr2


# this does not work and possibly even slower
def xcorNonLinear_para(arr, ref, npxls=32, threshold=None, phaseContrast=True, cthre=CTHRE):
    """
    arr: image to be registered
    ref: iamge to find the alignment parameter
    nplxs: number of pixels to divide (y,x) or scaler
    threshold: threshold value to decide if the region is to be cross correlated
    pahseContrast: phase contrast filter in cross correlation

    return (yx_arr, px_analyzed_arr[bool,var,cqual], result_arr)
    """
    from PriCommon import ppro26
    
    try:
        if len(npxls) != len(arr.shape):
            raise ValueError('length of the list of npxls must be the same as len(shape)')
    except TypeError:
        npxls = [npxls for d in range(len(arr.shape))]
    
    tslcs, arrs = chopImage2D(arr, npxls)
    rslcs, refs = chopImage2D(ref, npxls)


    if threshold is None:
        variance = (arr.var() + ref.var()) / 2.
        threshold = variance * 0.1

    nsplit = (len(tslcs), len(tslcs[0]))
    yxs = N.zeros((2,)+tuple(nsplit), N.float32)
    regions = N.zeros((3,)+tuple(nsplit), N.float32)
    cs = N.zeros_like(arr)

    abyxs = []
    for y, ay in enumerate(arrs):
        for x, a in enumerate(ay):
            b = refs[y][x]
            abyxs.append((a, b, y, x))
            old="""

            av = imgFilters.cutOutCenter(a, 0.5, interpolate=False)
            bv = imgFilters.cutOutCenter(b, 0.5, interpolate=False)
            var = (N.var(av) + N.var(bv)) / 2. # crop to throw away the tip object
            regions[1,y,x] = var
            if var > threshold:
                yx, c = xcorr.Xcorr(a, b, phaseContrast=phaseContrast)
                cs[tslcs[y][x]] = c
                csd = c[:c.shape[0]//4].std()
                cqual = c.max() - csd
                regions[2,y,x] = cqual
                if cqual >= cthre: # 0.3 is the max
                    yxs[:,y,x] = yx
                    regions[0,y,x] = 1
                del c"""


    if 0:#ppro26.NCPU > 1:
        print('multi')
        ccy = ppro26.pmap(_xcorNonLinear, abyxs, threshold=threshold, phaseContrast=phaseContrast)
    else:
        print('single')
        ccy = [_xcorNonLinear(ab, threshold=threshold, phaseContrast=phaseContrast) for ab in abyxs]

    for c, var, cqual, yx, y, x in ccy:
        regions[1,y,x] = var
        if var > threshold:
            regions[2,y,x] = cqual
            if cqual >= cthre:
                yxs[:,y,x] = yx
                regions[0,y,x] = 1
                cs[tslcs[y][x]] = c
            del c

    del arrs, refs#, c
    return yxs, regions, cs

def _xcorNonLinear(abyx, threshold, phaseContrast):
    a, b, y, x = abyx
    av = imgFilters.cutOutCenter(a, 0.5, interpolate=False)
    bv = imgFilters.cutOutCenter(b, 0.5, interpolate=False)
    var = (N.var(av) + N.var(bv)) / 2. # crop to throw away the tip object
    #regions[1,y,x] = var
    if var > threshold:
        yx, c = xcorr.Xcorr(a, b, phaseContrast=phaseContrast)
        #cs[tslcs[y][x]] = c
        csd = c[:c.shape[0]//4].std()
        cqual = c.max() - csd
        #regions[2,y,x] = cqual
        #if cqual >= cthre: # 0.3 is the max
            #yxs[:,y,x] = yx
            #regions[0,y,x] = 1
            # del c
    else:
        c = None
        cqual = 0
        yx = 0
    return c, var, cqual, yx, y, x

def fillHoles(yx, region, win=3):
    """
    fills empty regions for local alignment

    return filled yx
    """
    if region.max() == 0:
        return yx
    
    while region.min() == 0:
        region2 = N.copy(region)
        half = win//2
        ny, nx = region.shape
        yx2 = N.copy(yx)

        for y, yr in enumerate(region):
            for x, xr in enumerate(yr):
                yh0 = xh0 = half
                yh1 = xh1 = half + 1
                # if this region is empty...
                if not xr:
                    # edge treatment
                    if y < half:
                        yh0 = 0
                    elif y > (ny - half):
                        yh1 = 1

                    if x < half:
                        xh0 = 0
                    elif x > (nx - half):
                        xh1 = 1

                    # indices of regions with values
                    yi, xi = N.nonzero(region[y-yh0:y+yh1,x-xh0:x+xh1])
                    if len(yi):
                        # replace with mean in the window
                        yx2[0,y,x] = N.mean(yx[0,y-yh0:y+yh1,x-xh0:x+xh1][yi,xi]) / (2 ** ((win-3)/2))
                        yx2[1,y,x] = N.mean(yx[1,y-yh0:y+yh1,x-xh0:x+xh1][yi,xi]) / (2 ** ((win-3)/2))
                        region2[y,x] = 1
        region = region2
        yx = yx2
        win += 2
    return yx
            
