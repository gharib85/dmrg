'''
DMRG Engine.
'''

from numpy import *
from scipy.sparse.linalg import eigsh
from scipy.linalg import eigh,svd,eigvalsh
from numpy.linalg import norm
from numpy import kron as dkron
from matplotlib.pyplot import *
import scipy.sparse as sps
import copy,time,pdb,warnings,numbers

from blockmatrix.blocklib import eigbsh,eigbh,get_blockmarker,svdb
from tba.hgen import SpinSpaceConfig,ind2c,Z4scfg
from rglib.mps import MPS,OpString,tensor,insert_Zs
from rglib.hexpand import NullEvolutor,MaskedEvolutor
from tba.hgen import kron_csr as kron
from blockmatrix import SimpleBMG,sign4bm,show_bm,trunc_bm
from disc_symm import SymmetryHandler
from superblock import SuperBlock,site_image,joint_extract_block
from pydavidson import JDh
from flib.flib import fget_subblock_dmrg

__all__=['site_image','SuperBlock','DMRGEngine','fix_tail']

ZERO_REF=1e-12

def _eliminate_zeros(A,zero_ref):
    '''eliminate zeros from a sparse matrix.'''
    if not isinstance(A,sps.csr_matrix): A=A.tocsr()
    A.data[abs(A.data)<zero_ref]=0; A.eliminate_zeros()
    return A

def _gen_hamiltonian_full(HL0,HR0,hgen_l,hgen_r,interop):
    '''Get the full hamiltonian.'''
    ndiml,ndimr=HL0.shape[0],HR0.shape[0]
    H1,H2=kron(HL0,sps.identity(ndimr)),kron(sps.identity(ndiml),HR0)
    H=H1+H2
    #get the link hamiltonians
    sb=SuperBlock(hgen_l,hgen_r)
    Hin=[]
    for op in interop:
        Hin.append(sb.get_op(op))
    H=H+sum(Hin)
    H=_eliminate_zeros(H,ZERO_REF)
    return H

def _gen_hamiltonian_block0(HL0,HR0,hgen_l,hgen_r,interop,blockinfo):
    '''Get the combined hamiltonian for specific block.'''
    ndiml,ndimr=HL0.shape[0],HR0.shape[0]
    bml,bmr,pml,pmr,bmg,target_block=blockinfo['bml'],blockinfo['bmr'],blockinfo['pml'],blockinfo['pmr'],blockinfo['bmg'],blockinfo['target_block']
    bm_tot,pm=bmg.join_bms([bml,bmr]).compact_form()
    pm=((pml*len(pmr))[:,newaxis]+pmr).ravel()[pm]
    t0=time.time()
    H1,H2=kron(HL0,sps.identity(ndimr)),kron(sps.identity(ndiml),HR0)
    t1=time.time()
    indices=pm[bm_tot.get_slice(target_block,uselabel=True)]
    H1,H2=H1.tocsr()[indices][:,indices],H2.tocsr()[indices][:,indices]
    Hc=H1+H2
    sb=SuperBlock(hgen_l,hgen_r)
    for op in interop:
        Hc=Hc+(sb.get_op(op)).tocsr()[indices][:,indices]
    t2=time.time()
    print 'Generate Hamiltonian %s, %s'%(t1-t0,t2-t1)
    return Hc,bm_tot,pm

def _gen_hamiltonian_block(HL0,HR0,hgen_l,hgen_r,interop,blockinfo):
    '''Get the combined hamiltonian for specific block.'''
    ndiml,ndimr=HL0.shape[0],HR0.shape[0]
    bm_tot,pm=blockinfo['bmg'].join_bms([blockinfo['bml'],blockinfo['bmr']]).compact_form()
    pm=((blockinfo['pml']*ndimr)[:,newaxis]+blockinfo['pmr']).ravel()[pm]
    indices=pm[bm_tot.get_slice(blockinfo['target_block'],uselabel=True)]
    cinds=ind2c(indices,N=[ndiml,ndimr])
    t0=time.time()
    H1=fget_subblock_dmrg(hl=HL0.toarray(),hr=identity(ndimr),indices=cinds,is_identity=2)
    H2=fget_subblock_dmrg(hl=identity(ndiml),hr=HR0.toarray(),indices=cinds,is_identity=1)
    Hc=H1+H2
    t1=time.time()
    sb=SuperBlock(hgen_l,hgen_r)
    for op in interop:
        Hc=Hc+sb.get_op(op,indices=cinds)
    t2=time.time()
    print 'Generate Hamiltonian %s, %s'%(t1-t0,t2-t1)
    return sps.csr_matrix(Hc),bm_tot,pm

def _get_mps(hgen_l,hgen_r,phi,direction,labels):
    '''Combining hgen_l and hgen_r to get the matrix product state.'''
    NL,NR=hgen_l.N,hgen_r.N
    phi=tensor.Tensor(phi,labels=['al','sl+1','al+2','sl+2']) #l=NL-1
    if direction=='->':
        A=hgen_l.evolutor.A(NL-1,dense=True)   #get A[sNL](NL-1,NL)
        A=tensor.Tensor(A,labels=['sl+1','al','al+1\''])
        phi=tensor.contract([A,phi])
        phi=phi.chorder([0,2,1])   #now we get phi(al+1,sl+2,al+2)
        #decouple phi into S*B, B is column-wise othorgonal
        U,S,V=svd(phi.reshape([phi.shape[0],-1]),full_matrices=False)
        U=tensor.Tensor(U,labels=['al+1\'','al+1'])
        A=(A*U)  #get A(al,sl+1,al+1)
        B=transpose(V.reshape([S.shape[0],phi.shape[1],phi.shape[2]]),axes=(1,2,0))   #al+1,sl+2,al+2 -> sl+2,al+2,al+1, stored in column wise othorgonal format
    else:
        B=hgen_r.evolutor.A(NR-1,dense=True)   #get B[sNR](NL+1,NL+2)
        B=tensor.Tensor(B,labels=['sl+2','al+2','al+1\'']).conj()    #!the conjugate?
        phi=tensor.contract([phi,B])
        #decouple phi into A*S, A is row-wise othorgonal
        U,S,V=svd(phi.reshape([phi.shape[0]*phi.shape[1],-1]),full_matrices=False)
        V=tensor.Tensor(V,labels=['al+1','al+1\''])
        B=(V*B).chorder([1,2,0]).conj()   #al+1,sl+2,al+2 -> sl+2,al+2,al+1, for B is in transposed order by default.
        A=transpose(U.reshape([phi.shape[0],phi.shape[1],S.shape[0]]),axes=(1,0,2))   #al,sl+1,al+1 -> sl+1,al,al+1, stored in column wise othorgonal format

    AL=hgen_l.evolutor.get_AL(dense=True)[:-1]+[A]
    BL=[B]+hgen_r.evolutor.get_AL(dense=True)[::-1][1:]

    AL=[transpose(ai,axes=(1,0,2)) for ai in AL]
    BL=[transpose(bi,axes=(1,0,2)).conj() for bi in BL]   #transpose
    mps=MPS(AL=AL,BL=BL,S=S,labels=labels,forder=range(NL)+range(NL,NL+NR)[::-1])
    return mps

class DMRGEngine(object):
    '''
    DMRG Engine.

    Attributes:
        :hgen: <ExpandGenerator>, hamiltonian Generator.
        :bmg: <BlockMarkerGenerator>, the block marker generator.
        :tol: float, the tolerence, when maxN and tol are both set, we keep the lower dimension.
        :reflect: bool, True if left<->right reflect, can be used to shortcut the run time.
        :eigen_solver: str,
            
            * 'JD', Jacobi-Davidson iteration.
            * 'LC', Lanczos, algorithm.
        :iprint: int, the redundency level of output information, 0 for None, 10 for debug.

        :symm_handler: <SymmetryHandler>, the discrete symmetry handler.
        :LPART/RPART: dict, the left/right sweep of hamiltonian generators.
        :_tails(private): list, the last item of A matrices, which is used to construct the <MPS>.
    '''
    def __init__(self,hgen,tol=0,reflect=False,eigen_solver='LC',iprint=1):
        self.tol=tol
        self.hgen=hgen
        self.eigen_solver=eigen_solver

        #the symmetries
        self.reflect=reflect
        self.bmg=None
        self._target_block=None
        self.symm_handler=SymmetryHandler({},detect_scope=1)

        #claim attributes with dummy values.
        self._tails=None
        self.LPART=None
        self.RPART=None

        self.iprint=iprint
        #status
        self.status={'isweep':0,'direction':'->','pos':0}

    def _eigsh(self,H,v0,projector=None,tol=1e-10,sigma=None,lc_search_space=1,k=1):
        '''
        solve eigenvalue problem.
        '''
        maxiter=5000
        N=H.shape[0]
        if self.iprint==10 and projector is not None and check_commute:
            assert(is_commute(H,projector))
        if self.eigen_solver=='LC':
            k=max(lc_search_space,k)
            if H.shape[0]<100:
                e,v=eigh(H.toarray())
                e,v=e[:k],v[:,:k]
            else:
                try:
                    e,v=eigsh(H,k=k,which='SA',maxiter=maxiter,tol=tol,v0=v0)
                except:
                    e,v=eigsh(H,k=k+1,which='SA',maxiter=maxiter,tol=tol,v0=v0)
            order=argsort(e)
            e,v=e[order],v[:,order]
        else:
            iprint=0
            maxiter=500
            if projector is not None:
                e,v=JDh(H,v0=v0,k=k,projector=projector,tol=tol,maxiter=maxiter,sigma=sigma,which='SA',iprint=iprint)
            else:
                if sigma is None:
                    e,v=JDh(H,v0=v0,k=max(lc_search_space,k),projector=projector,tol=tol,maxiter=maxiter,which='SA',iprint=iprint)
                else:
                    e,v=JDh(H,v0=v0,k=k,projector=projector,tol=tol,sigma=sigma,which='SL',\
                            iprint=iprint,converge_bound=1e-10,maxiter=maxiter)

        nstate=len(e)
        if nstate==0:
            raise Exception('No Converged Pair!!')
        elif nstate==k or k>1:
            return e,v

        #filter out states meeting projector.
        if projector is not None and lc_search_space!=1:
            overlaps=array([abs(projector.dot(v[:,i]).conj().dot(v[:,i])) for i in xrange(nstate)])
            mask0=overlaps>0.1
            if not any(mask0):
                raise Exception('Can not find any states meeting specific parity!')
            mask=overlaps>0.9
            if sum(mask)==0:
                #check for degeneracy.
                istate=where(mask0)[0][0]
                warnings.warn('Wrong result or degeneracy accur!')
            else:
                istate=where(mask)[0][0]
            v=projector.dot(v[:,istate:istate+1])
            v=v/norm(v)
            return e[istate:istate+1],v
        else:
            #get the state with maximum overlap.
            v0H=v0.conj()/norm(v0)
            overlaps=array([abs(v0H.dot(v[:,i])) for i in xrange(nstate)])
            istate=argmax(overlaps)
            if overlaps[istate]<0.7:
                warnings.warn('Do not find any states same correspond to the one from last iteration!%s'%overlaps)
        e,v=e[istate:istate+1],v[:,istate:istate+1]
        return e,v

    @property
    def nsite(self):
        '''Number of sites'''
        return self.hgen.nsite

    def query(self,which,length):
        '''
        Query the hamiltonian generator of specific part.

        which:
            `l` -> the left part.
            `r` -> the right part.
        length:
            The length of block.
        '''
        assert(which=='l' or which=='r')
        if which=='l' or self.reflect:
            return copy.copy(self.LPART[length])
        else:
            return copy.copy(self.RPART[length])

    def set(self,which,hgen,length=None):
        '''
        Set the hamiltonian generator for specific part.

        Parameters:
            :which: str,

                * `l` -> the left part.
                * `r` -> the right part.
            :hgen: <ExpandGenerator>, the RG hamiltonian generator.
            :length: int, the length of block, if set, it will do a length check.
        '''
        assert(length is None or length==hgen.N)
        assert(hgen.truncated)
        if which=='l' or self.reflect:
            self.LPART[hgen.N]=hgen
        else:
            self.RPART[hgen.N]=hgen

    def reset(self):
        '''Restore this engine to initial status.'''
        #we insert Zs into operator collections to cope with fermionic sign problem.
        #and use site image to create a reversed ordering!
        hgen_l=copy.deepcopy(self.hgen)
        if not isinstance(hgen_l.spaceconfig,SpinSpaceConfig):
            insert_Zs(hgen_l.evolutees['H'].opc,spaceconfig=hgen_l.spaceconfig)
        self.LPART={0:hgen_l}
        if not self.reflect:
            hgen_r=copy.deepcopy(self.hgen)
            hgen_r.evolutees['H'].opc=site_image(hgen_r.evolutees['H'].opc,NL=0,NR=hgen_r.nsite,care_sign=True)
            if not isinstance(hgen_l.spaceconfig,SpinSpaceConfig):
                insert_Zs(hgen_r.evolutees['H'].opc,spaceconfig=hgen_r.spaceconfig)
            self.RPART={0:hgen_r}

    def use_disc_symmetry(self,target_sector,detect_scope=2):
        '''
        Use specific discrete symmetry.

        Parameters:
            :target_sector: dict, {name:parity} pairs.
            :detect_scope:
        '''
        if target_sector.has_key('C') and not self.reflect:
            raise Exception('Using C2 symmetry without reflection symmetry is unreliable, forbiden for safety!')
        symm_handler=SymmetryHandler(target_sector,detect_scope=detect_scope)
        if target_sector.has_key('P'):  #register flip evolutee.
            handler=symm_handler.handlers['P']
            self.hgen.register_evolutee('P',opc=prod([handler.P(i) for i in xrange(self.hgen.nsite)]),initial_data=sps.identity(1))
        if target_sector.has_key('J'):  #register p-h evolutee.
            handler=symm_handler.handlers['J']
            self.hgen.register_evolutee('J',opc=prod([handler.J(i) for i in xrange(self.hgen.nsite)]),initial_data=sps.identity(1))
        self.symm_handler=symm_handler

    def use_U1_symmetry(self,qnumber,target_block):
        '''
        Use specific U1 symmetry.
        '''
        self.bmg=SimpleBMG(spaceconfig=self.hgen.spaceconfig,qstring=qnumber)
        self._target_block=target_block

    @property
    def target_block(self):
        '''Get the target block.'''
        target_block=self._target_block
        if hasattr(target_block,'__call__'):
            n,pos=self.status['isweep'],self.status['pos']
            nsite=self.nsite
            if n==0 and pos<nsite/2: nsite=pos*2
            target_block=target_block(nsite=nsite)
        return target_block

    def run_finite(self,endpoint=None,tol=0,maxN=20,nlevel=1,call_before=None,call_after=None):
        '''
        Run the application.

        Parameters:
            :endpoint: tuple, the end position tuple of (sweep, direction, size of left-block).
            :tol: float, the rolerence of energy.
            :maxN: int, maximum number of kept states and the tolerence for truncation weight.
            :nlevel: int, the number of desired energy levels.
            :call_before/call_after: function/None, the function to call back before/after each iteration, using `DMRGEngine` as an parameter.

        Return:
            tuple, the ground state energy and the ground state(in <MPS> form).
        '''
        EL=[]
        #check the validity of datas.
        if isinstance(self.hgen.evolutor,NullEvolutor):
            raise ValueError('The evolutor must not be null!')
        if not self.symm_handler==None and nlevel!=1:
            raise NotImplementedError('The symmetric Handler can not be used in multi-level calculation!')
        if not self.symm_handler==None and self.bmg is None:
            raise NotImplementedError('The symmetric Handler can not without Block marker generator!')
        self.reset()

        nsite=self.hgen.nsite
        if endpoint is None: endpoint=(4,'<-',0)
        maxsweep,end_direction,end_site=endpoint
        if ndim(maxN)==0:
            maxN=[maxN]*maxsweep
        assert(len(maxN)>=maxsweep and end_site<=(nsite-2 if not self.reflect else nsite/2-2))
        EG_PRE=Inf
        initial_state=None
        if self.reflect:
            iterators={'->':xrange(nsite/2),'<-':xrange(nsite/2-2,-1,-1)}
        else:
            iterators={'->':xrange(nsite-1),'<-':xrange(nsite-2,-1,-1)}
        for n,m in enumerate(maxN):
            for direction in ['->','<-']:
                for i in iterators[direction]:
                    print 'Running %s-th sweep, iteration %s'%(n+1,i)
                    t0=time.time()
                    self.status.update({'isweep':n,'pos':i+1,'direction':direction})
                    if call_before is not None: call_before(self)
                    #setup generators and operators.
                    #The cases to use identical hamiltonian generator,
                    #1. the first half of first sweep.
                    #2. the reflection is used and left block is same length with right block.
                    hgen_l=self.query('l',i)
                    if (n==0 and direction=='->' and i<(nsite+1)/2) or (self.reflect and i==(nsite/2-1) and nsite%2==0):
                        hgen_r=hgen_l
                    else:
                        hgen_r=self.query('r',nsite-i-2)
                    print 'A'*hgen_l.N+'..'+'B'*hgen_r.N
                    nsite_true=hgen_l.N+hgen_r.N+2

                    #run a step
                    if n<=2:
                        e_estimate=None
                    else:
                        e_estimate=EG[0]
                    EG,err,phil=self.dmrg_step(hgen_l,hgen_r,tol=tol,maxN=m,
                            initial_state=initial_state,e_estimate=e_estimate,nlevel=nlevel)
                    #update LPART and RPART
                    print 'setting %s-site of left and %s-site of right.'%(hgen_l.N,hgen_r.N)
                    self.set('l',hgen_l,hgen_l.N)
                    print 'set L = %s, size %s'%(hgen_l.N,hgen_l.ndim)
                    if hgen_l is not hgen_r or (not self.reflect and n==0 and i<nsite/2):
                        #Note: Condition for setting up the right block,
                        #1. when the left and right part are not the same one.
                        #2. when the block has not been expanded to full length and not reflecting.
                        self.set('r',hgen_r,hgen_r.N)
                        print 'set R = %s, size %s'%(hgen_r.N,hgen_r.ndim)
                    if call_after is not None: call_after(self)

                    #do state prediction
                    initial_state=None   #restore initial state.
                    phi=phil[0]
                    if nsite==nsite_true:
                        if self.reflect and nsite%2==0 and (i==nsite/2-2 and direction=='->'):
                            #Prediction can not be used:
                            #when we are going to calculate the symmetry point
                            #and use the reflection symmetry.
                            #for the right block is instantly replaced by another hamiltonian generator,
                            #which is not directly connected to the current hamiltonian generator.
                            initial_state=sum([self.state_prediction(phi,l=i+1,direction=direction) for phi in phil],axis=0).ravel()
                        elif direction=='->' and i==nsite-2:  #for the case without reflection.
                            initial_state=phil[0].ravel()
                        elif direction=='<-' and i==0:
                            initial_state=phil[0].ravel()
                        else:
                            if self.reflect and direction=='->' and i==nsite/2-1:
                                direction='<-'  #the turning point of where reflection used.
                            initial_state=sum([self.state_prediction(phi,l=i+1,direction=direction) for phi in phil],axis=0)
                            initial_state=initial_state.ravel()

                    if len(EL)>0:
                        diff=EG-EL[-1]
                    else:
                        diff=Inf
                    t1=time.time()
                    print 'EG = %s, dE = %s, Elapse -> %.2f, TruncError -> %s'%(EG,diff,t1-t0,err)
                    EL.append(EG)
                    if i==end_site and direction==end_direction:
                        diff=EG-EG_PRE
                        print 'MidPoint -> EG = %s, dE = %s'%(EG,diff)
                        if n==maxsweep-1:
                            print 'Breaking due to maximum sweep reached!'
                            return EG,self.get_mps(phi=phil[0],l=i+1,direction=direction)
                        else:
                            EG_PRE=EG

    def run_infinite(self,maxiter=50,tol=0,maxN=20,nlevel=1):
        '''
        Run the application.

        Parameters:
            :maxiter: int, the maximum iteration times.
            :tol: float, the rolerence of energy.
            :maxN: int/list, maximum number of kept states and the tolerence for truncation weight.

        Return:
            tuple of EG,MPS.
        '''
        if isinstance(self.hgen.evolutor,NullEvolutor):
            raise ValueError('The evolutor must not be null!')
        if not self.symm_handler==None and nlevel!=1:
            raise NotImplementedError('The symmetric Handler can not be used in multi-level calculation!')
        if not self.symm_handler==None and self.bmg is None:
            raise NotImplementedError('The symmetric Handler can not without Block marker generator!')
        self.reset()

        EL=[]
        hgen=copy.deepcopy(self.hgen)
        if isinstance(hgen.evolutor,NullEvolutor):
            raise ValueError('The evolutor must not be null!')
        if maxiter>self.hgen.nsite:
            warnings.warn('Max iteration exceeded the chain length!')
        for i in xrange(maxiter):
            print 'Running iteration %s'%i
            t0=time.time()
            EG,err,phil=self.dmrg_step(hgen,hgen,tol=tol,nlevel=nlevel)
            EG=EG/(2.*(i+1))
            if len(EL)>0:
                diff=EG-EL[-1]
            else:
                diff=Inf
            t1=time.time()
            print 'EG = %.5f, dE = %.2e, Elapse -> %.2f(D=%s), TruncError -> %.2e'%(EG,diff,t1-t0,hgen.ndim,err)
            EL.append(EG)
            if abs(diff)<tol:
                print 'Breaking!'
                break
        return EG,_get_mps(hgen,hgen,phi=phil[0],direction='->',labels=['s','a'])

    def dmrg_step(self,hgen_l,hgen_r,tol=0,maxN=20,e_estimate=None,nlevel=1,initial_state=None):
        '''
        Run a single step of DMRG iteration.

        Parameters:
            :hgen_l,hgen_r: <ExpandGenerator>, the hamiltonian generator for left and right blocks.
            :tol: float, the rolerence.
            :maxN: int, maximum number of kept states and the tolerence for truncation weight.
            :initial_state: 1D array/None, the initial state(prediction), None for random.

        Return:
            tuple of (ground state energy(float), unitary matrix(2D array), kpmask(1D array of bool), truncation error(float))
        '''
        direction=self.status['direction']
        target_block=self.target_block

        t0=time.time()
        intraop_l,intraop_r,interop=[],[],[]
        hndim=hgen_l.hndim
        ndiml0,ndimr0=hgen_l.ndim,hgen_r.ndim
        NL,NR=hgen_l.N,hgen_r.N
        #filter operators to extract left-only and right-only blocks.
        interop=filter(lambda op:isinstance(op,OpString) and (NL+1 in op.siteindex),hgen_l.hchain.query(NL))  #site NL and NL+1
        OPL=hgen_l.expand1()
        HL0=OPL['H']
        #expansion can not do twice to the same hamiltonian generator!
        if hgen_r is hgen_l:
            OPR,HR0=OPL,HL0
        else:
            OPR=hgen_r.expand1()
            HR0=OPR['H']

        #blockize HL0 and HR0
        NL,NR=hgen_l.N,hgen_r.N
        if self.bmg is not None:
            n=max(NL,NR)
            if isinstance(hgen_l.evolutor,MaskedEvolutor) and n>1:
                kpmask_l=hgen_l.evolutor.kpmask(NL-2)     #kpmask is also related to block marker!!!
                kpmask_r=hgen_r.evolutor.kpmask(NR-2)
                bml,pml=self.bmg.update1(trunc_bm(hgen_l.block_marker or self.bmg.bm0,kpmask_l)).compact_form()
                bmr,pmr=self.bmg.update1(trunc_bm(hgen_r.block_marker or self.bmg.bm0,kpmask_r)).compact_form()
            else:
                bml,pml=self.bmg.update1(hgen_l.block_marker).compact_form()
                bmr,pmr=self.bmg.update1(hgen_r.block_marker).compact_form()
        else:
            bml,pml=None,None #get_blockmarker(HL0)
            bmr,pmr=None,None #get_blockmarker(HR0)

        if target_block is None:
            Hc,bm_tot=_gen_hamiltonian_full(HL0,HR0,hgen_l,hgen_r,interop=interop),None
        else:
            if False:    #efficiency cross over
                Hc,bm_tot,pm_tot=_gen_hamiltonian_block0(HL0,HR0,hgen_l=hgen_l,hgen_r=hgen_r,\
                        blockinfo=dict(bml=bml,bmr=bmr,pml=pml,pmr=pmr,bmg=self.bmg,target_block=target_block),interop=interop)
            else:
                Hc,bm_tot,pm_tot=_gen_hamiltonian_block(HL0,HR0,hgen_l=hgen_l,hgen_r=hgen_r,\
                        blockinfo=dict(bml=bml,bmr=bmr,pml=pml,pmr=pmr,bmg=self.bmg,target_block=target_block),interop=interop)

        #get the starting eigen state v00!
        if initial_state is None:
            initial_state=random.random(bm_tot.N)
        if not self.symm_handler==None:
            if hgen_l is not hgen_r:
                #Note, The cases to disable C2 symmetry,
                #1. NL!=NR
                #2. NL==NR, reflection is not used(and not the first iteration).
                self.symm_handler.update_handlers(OPL=OPL,OPR=OPR,useC=False)
            else:
                nl=(int32(1-sign4bm(bml,self.bmg,diag_only=True))/2)[argsort(pml)]
                self.symm_handler.update_handlers(OPL=OPL,OPR=OPR,n=nl,useC=True)
            v00=self.symm_handler.project_state(phi=initial_state)
            if self.iprint==10:assert(self.symm_handler.check_op(H))
        else:
            v00=initial_state

        #perform diagonalization
        ##1. detect specific block for diagonalization, get v0 and projector
        projector=self.symm_handler.get_projector() if len(self.symm_handler.symms)!=0 else None
        if self.bmg is None or target_block is None:
            v0=v00/norm(v00)
        else:
            indices=pm_tot[bm_tot.get_slice(target_block,uselabel=True)]
            v0=v00[indices]
            if projector is not None:
                projector=projector[indices]

        ##2. diagonalize to get desired number of levels
        detect_C2=self.symm_handler.target_sector.has_key('C')# and not symm_handler.useC
        t1=time.time()
        if norm(v0)==0:
            warnings.warn('Empty v0')
            v0=None
        print 'The density of Hamiltonian -> %s'%(1.*len(Hc.data)/Hc.shape[0]**2)
        e,v=self._eigsh(Hc,v0,sigma=e_estimate,projector=projector,
                lc_search_space=self.symm_handler.detect_scope if detect_C2 else 1,k=nlevel,tol=1e-10)
        if v0 is not None:
            print 'The goodness of estimate -> %s'%(v0.conj()/norm(v0)).dot(v[:,0])
        t2=time.time()
        ##3. permute back eigen-vectors into original representation al,sl+1,sl+2,al+2
        if bm_tot is not None:
            indices=pm_tot[bm_tot.get_slice(target_block,uselabel=True)]
            vl=zeros([bm_tot.N,v.shape[1]],dtype=v.dtype)
            vl[indices]=v; vl=vl.T
        else:
            vl=v.T

        #Do-wavefunction analysis, preliminary truncation is performed(up to ZERO_REF).
        for v in vl:
            v[abs(v)<ZERO_REF]=0
        #spec1,U1,kpmask1,trunc_error=self.rdm_analysis(phis=vl,bml=bml,bmr=bmr,side='l',maxN=maxN)
        U1,specs,U2,(kpmask1,kpmask2),trunc_error=self.svd_analysis(phis=vl,bml=HL0.shape[0] if bml is None else bml,\
                bmr=HR0.shape[0] if bmr is None else bmr,pml=pml,pmr=pmr,maxN=maxN)
        print '%s states kept.'%sum(kpmask1)
        hgen_l.trunc(U=U1,kpmask=kpmask1)  #kpmask is also important for setting up the sign
        if hgen_l is not hgen_r:
            #spec2,U2,kpmask2,trunc_error=self.rdm_analysis(phis=vl,bml=bml,bmr=bmr,side='r',maxN=maxN)
            hgen_r.trunc(U=U2,kpmask=kpmask2)
        phil=[phi.reshape([ndiml0,hndim,ndimr0,hndim]) for phi in vl]
        t3=time.time()
        print 'Elapse -> prepair:%.2f, eigen:%.2f, trunc: %.2f'%(t1-t0,t2-t1,t3-t2)
        return e,trunc_error,phil

    def svd_analysis(self,phis,bml,bmr,pml,pmr,maxN):
        '''
        The direct analysis of state(svd).
        
        Parameters:
            :phis: list of 1D array, the kept eigen states of current iteration.
            :bml/bmr: <BlockMarker>/int, the block marker for left and right blocks/or the dimensions.
            :maxN: int, the maximum kept values.

        Return:
            tuple of (spec, U), the spectrum and Unitary matrix from the density matrix.
        '''
        if isinstance(bml,numbers.Number):
            use_bm=False
            ndiml,ndimr=bml,bmr
        else:
            ndiml,ndimr=bml.N,bmr.N
            use_bm=True
        phi=sum(phis,axis=0).reshape([ndiml,ndimr])/sqrt(len(phis))  #construct wave function of equal distribution of all states.
        phi[abs(phi)<ZERO_REF]=0
        if use_bm:
            phi=phi[pml]
            phi=phi[:,pmr]
            def mapping_rule(bli):
                res=self.bmg.bcast_sub([self.target_block],[bli])[0]
                return tuple(res)
            U,S,V,S2=svdb(phi,bm=bml,bm2=bmr,mapping_rule=mapping_rule,full_matrices=True)
        else:
            U,S,V=svd(phi,full_matrices=True);U2=V.T.conj()
            if ndimr>=ndiml:
                S2=append(S,zeros(ndimr-ndiml))
            else:
                S2=append(S,zeros(ndiml-ndimr))
                S,S2=S2,S
            S,S2=sps.diags(S,0),sps.diags(S2,0)

        spec_l=S.dot(S.T.conj()).diagonal().real
        spec_r=S2.T.conj().dot(S2).diagonal().real

        if use_bm:
            if self.iprint==10 and not (bml.check_blockdiag(U.dot(sps.diags(spec_l,0)).dot(U.T.conj())) and\
                    bmr.check_blockdiag((V.T.conj().dot(sps.diags(spec_r,0))).dot(V))):
                raise Exception('''Density matrix is not block diagonal, which is not expected,
            1. make sure your are using additive good quantum numbers.
            2. avoid ground state degeneracy.''')
            #permute U and V
            U,V=U.tocsr()[argsort(pml)],V.tocsc()[:,argsort(pmr)]
            U2=V.T.conj()
        kpmasks=[]
        for Ui,spec in zip([U,U2],[spec_l,spec_r]):
            kpmask=zeros(Ui.shape[1],dtype='bool')
            spec_cut=sort(spec)[max(0,Ui.shape[0]-maxN)]
            kpmask[(spec>=spec_cut)&(spec>ZERO_REF)]=True
            trunc_error=sum(spec[~kpmask])
            kpmasks.append(kpmask)
        U,U2=_eliminate_zeros(U,ZERO_REF),_eliminate_zeros(U2,ZERO_REF)

        return U,(spec_l,spec_r),U2,kpmasks,trunc_error

    def rdm_analysis(self,phis,bml,bmr,side,maxN):
        '''
        The analysis of reduced density matrix.
        
        Parameters:
            :phis: list of 1D array, the kept eigen states of current iteration.
            :bml/bmr: <BlockMarker>/int, the block marker for left and right blocks/or the dimensions.
            :side: 'l'/'r', view the left or right side as the system.
            :maxN: the maximum kept values.

        Return:
            tuple of (spec, U), the spectrum and Unitary matrix from the density matrix.
        '''
        assert(side=='l' or side=='r')
        ndiml,ndimr=(bml,bmr) if isinstance(bml,numbers.Number) else (bml.N,bmr.N)
        phis=[phi.reshape([ndiml,ndimr]) for phi in phis]
        rho=0
        phil=[]
        if side=='l':
            for phi in phis:
                phi=sps.csr_matrix(phi)
                rho=rho+phi.dot(phi.T.conj())
                phil.append(phi)
            bm=bml
        else:
            for phi in phis:
                phi=sps.csc_matrix(phi)
                rho=rho+phi.T.dot(phi.conj())
                phil.append(phi)
            bm=bmr
        if bm is not None:
            rho=bm.blockize(rho)
            if self.iprint==10 and not bm.check_blockdiag(rho,tol=1e-5):
                ion()
                pcolor(exp(abs(rho.toarray().real)))
                show_bm(bm)
                pdb.set_trace()
                raise Exception('''Density matrix is not block diagonal, which is not expected,
        1. make sure your are using additive good quantum numbers.
        2. avoid ground state degeneracy.''')
        spec,U=eigbh(rho,bm=bm)
        kpmask=zeros(U.shape[1],dtype='bool')
        spec_cut=sort(spec)[max(0,U.shape[0]-maxN)]
        kpmask[(spec>=spec_cut)&(spec>ZERO_REF)]=True
        trunc_error=sum(spec[~kpmask])
        print 'With %s(%s) blocks.'%(bm.nblock,bm.nblock)
        return spec,U,kpmask,trunc_error

    def state_prediction(self,phi,l,direction):
        '''
        Predict the state for the next iteration.

        Parameters:
            :phi: ndarray, the state from the last iteration, [llink, site1, rlink, site2]
            :l: int, the current division point, the size of left block.
            :direction: '->'/'<-', the moving direction.

        Return:
            ndarray, the new state in the basis |al+1,sl+2,sl+3,al+3>.

            reference -> PRL 77. 3633
        '''
        assert(direction=='<-' or direction=='->')
        nsite=self.hgen.nsite
        NL,NR=l,nsite-l
        phi=tensor.Tensor(phi,labels=['a_%s'%(NL-1),'s_%s'%(NL),'b_%s'%(NR-1),'t_%s'%NR]) #l=NL-1
        if self.reflect and nsite%2==0 and l==nsite/2-1 and direction=='->':   #hard prediction!
            return self._state_prediction_hard(phi)
        hgen_l,hgen_r=self.query('l',NL),self.query('r',NR)
        lr=NR-2 if direction=='->' else NR-1
        ll=NL-1 if direction=='->' else NL-2
        A=hgen_l.evolutor.A(ll,dense=True)   #get A[sNL](NL-1,NL)
        B=hgen_r.evolutor.A(lr,dense=True)   #get B[sNR](NL+1,NL+2)
        if direction=='->':
            A=tensor.Tensor(A,labels=['s_%s'%NL,'a_%s'%(NL-1),'a_%s'%NL]).conj()
            B=tensor.Tensor(B,labels=['t_%s'%(NR-1),'b_%s'%(NR-2),'b_%s'%(NR-1)])#.conj()    #!the conjugate? right side shrink, so B(al,al+1) do not conjugate.
            phi=tensor.contract([A,phi,B])
            phi=phi.chorder([0,1,3,2])
            if hgen_r.use_zstring:  #cope with the sign problem
                n1=(1-Z4scfg(hgen_l.spaceconfig).diagonal())/2
                nr=(1-hgen_r.zstring(lr).diagonal())/2
                n_tot=n1[:,newaxis,newaxis]*(nr[:,newaxis]+n1)
                phi=phi*(1-2*(n_tot%2))
        else:
            A=tensor.Tensor(A,labels=['s_%s'%(NL-1),'a_%s'%(NL-2),'a_%s'%(NL-1)])#.conj()
            B=tensor.Tensor(B,labels=['t_%s'%NR,'b_%s'%(NR-1),'b_%s'%NR]).conj()    #!the conjugate?
            phi=tensor.contract([A,phi,B])
            phi=phi.chorder([1,0,3,2])
            if hgen_r.use_zstring:  #cope with the sign problem
                n1=(1-Z4scfg(hgen_l.spaceconfig).diagonal())/2
                nr=(1-hgen_r.zstring(lr+1).diagonal())/2
                n_tot=n1*(nr[:,newaxis])
                phi=phi*(1-2*(n_tot%2))
        return phi

    def _state_prediction_hard(self,phi):
        '''
        The hardest prediction for reflection point for phi(al,sl+1,sl+2,al+2) -> phi(al-1,sl,sl+1,al+1')
        '''
        nsite=self.hgen.nsite
        l=nsite/2
        hgen_l,hgen_r0,hgen_r=self.query('l',l-1),self.query('r',l+2),self.query('r',l-1)
        #do regular evolution to phi(al,sl+1,sl+2,al+2) -> phi(al-1,sl,sl+1,al+1)
        A=hgen_l.evolutor.A(l-2,dense=True)   #get A[sNL](NL-1,NL)
        B=hgen_r0.evolutor.A(l-1,dense=True)   #get B[sNR](NL+1,NL+2)
        A=tensor.Tensor(A,labels=['s_%s'%(l-1),'a_%s'%(l-2),'a_%s'%(l-1)])
        B=tensor.Tensor(B,labels=['t_%s'%l,'b_%s'%(l-1),'b_%s'%(l)]).conj()
        phi=tensor.contract([A,phi,B])
        if hgen_r.use_zstring:  #cope with the sign problem
            n1=(1-Z4scfg(hgen_l.spaceconfig).diagonal())/2
            nr=(1-hgen_r0.zstring(l-1).diagonal())/2
            n_tot=n1[:,newaxis,newaxis]*(nr+n1[:,newaxis])
            phi=phi*(1-2*(n_tot%2))
        #do the evolution from phi(al-1,sl,sl+1,al+1) -> phi(al-1,sl,sl+1,al+1')
        #first calculate tensor R(al+1',al+1), right one incre, left decre.
        BL0=hgen_r0.evolutor.get_AL(dense=True)[:l-1]
        BL=hgen_r.evolutor.get_AL(dense=True)
        BL0=[tensor.Tensor(bi,labels=['t_%s'%(i+1),'b_%s'%i,'b_%s'%(i+1)]) for i,bi in enumerate(BL0)]
        BL=[tensor.Tensor(bi,labels=['t_%s'%(i+1),'b_%s'%i+('\'' if i!=0 else ''),'b_%s\''%(i+1)]).conj() for i,bi in enumerate(BL)]
        R=BL[0]*BL0[0]
        for i in xrange(1,l-1):
            R=tensor.contract([R,BL0[i],BL[i]])
        #second, calculate phi*R
        phi=phi*R

        phi=phi.chorder([0,1,3,2])
        return phi

    def get_mps(self,phi,l,labels=['s','a'],direction=None):
        '''
        Get the MPS from run-time phi, and evolution matrices.

        Parameters:
            :phi: ndarray, the eigen-function of current step.
            :l: int, the size of left block.
            :direction: '->'/'<-'/None, if None, the direction is provided by the truncation information.

        Return:
            <MPS>, the disired MPS, the canonicallity if decided by the current position.
        '''
        #get the direction
        assert(direction=='<-' or direction=='->')
        nsite=self.hgen.nsite
        NL,NR=l,nsite-l
        hgen_l,hgen_r=self.query('l',NL),self.query('r',NR)
        return _get_mps(hgen_l,hgen_r,phi,direction,labels)

def fix_tail(mps,spaceconfig,parity,head2tail=True):
    '''
    Fix the ordering to normal order(reverse).

    Parameters:
        :mps: <MPS>, the matrix product state.
        :spaceconfig: <SpaceConfig>,
        :parity: int, 1 for odd parity, 0 for even parity.
        :head2tail: bool, move head to tail if True, else move tail to head.

    Return:
        <MPS>, the new MPS.
    '''
    nsite=mps.nsite
    assert(allclose(mps.forder,[0]+range(1,nsite)[::-1]))
    n1=(1-Z4scfg(spaceconfig).diagonal())/2
    site_axis=mps.site_axis
    if head2tail:
        j=list(mps.forder).index(0)
        norder=array(mps.forder)-1
        norder[j]=nsite-1
    else:
        j=list(mps.forder).index(nsite-1)
        norder=array(mps.forder)+1
        norder[j]=0
    mps.forder=norder
    if parity==1:
        return mps
    if j<mps.l:
        mps.AL[j]=mps.AL[j]*(1-2*(n1%2))[tuple([slice(None)]+[newaxis]*(2-site_axis))]
    else:
        mps.BL[j-mps.l]=mps.BL[j-mps.l]*(1-2*(n1%2))[tuple([slice(None)]+[newaxis]*(2-site_axis))]
    return mps


